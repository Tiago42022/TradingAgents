import argparse
import importlib
import json
import time

from .broker import Broker
from .common import Refusal
from .config import load_config
from .executor import Executor
from .journal import Journal, Singleton
from .transport import Transport


def main(argv=None):
    parser = argparse.ArgumentParser(description="Archive explicit manual MT5 adapter. No research/strategy execution.")
    parser.add_argument("--config", required=True, help="Protected external mt5-trading.json")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="Read-only identity/permission check; no queue or orders")
    group.add_argument("--once", action="store_true", help="One queue cycle; CAN execute a previously confirmed order")
    group.add_argument("--run", action="store_true", help="Poll only for explicitly confirmed manual orders")
    args = parser.parse_args(argv)
    broker = journal = lock = None
    try:
        config = load_config(args.config)
        lock = Singleton(config.journal_directory)
        broker = Broker(config, importlib.import_module("MetaTrader5"))
        broker.initialize()
        if args.check:
            account, permissions = broker.identity()
            print(json.dumps({"status": "verified", "accountLast4": config.expected_login[-4:],
                              "serverMatched": True, "currency": account.currency, "permissions": permissions,
                              "manualTradingConfigured": config.manual_trading}), flush=True)
            return 0
        journal = Journal(config.journal_directory)
        executor = Executor(config, broker, journal, Transport(config))
        previous = None
        while True:
            try:
                executor.step()
                status = {"status": "connected"}
            except Refusal as error:
                if error.fatal:
                    raise
                status = {"status": "unavailable", "code": error.code}
            if status != previous:
                print(json.dumps(status), flush=True)
                previous = status
            if args.once:
                return 0 if status["status"] == "connected" else 1
            time.sleep(2)
    except KeyboardInterrupt:
        print('{"status":"stopped"}', flush=True)
        return 0
    except Refusal as error:
        print(json.dumps({"status": "unavailable", "code": error.code}), flush=True)
        return 1
    except Exception:
        print('{"status":"unavailable","code":"executor_failed"}', flush=True)
        return 1
    finally:
        if broker:
            try:
                broker.shutdown()
            except Exception:
                pass
        if journal:
            journal.close()
        if lock:
            lock.close()
