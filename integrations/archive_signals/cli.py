"""Operator-started monitor; no startup task, broker credentials or trade actions."""
import argparse
import importlib
import json
import time

from .common import Refusal
from .config import load_config
from .monitor import CandleSource, Monitor
from .transport import Transport


def main(argv=None):
    parser = argparse.ArgumentParser(description="Archive read-only Gold M30 candle monitor")
    parser.add_argument("--config", required=True, help="Protected external mt5-signals.json")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--probe", action="store_true", help="Verify the bound terminal without network or candles")
    mode.add_argument("--run", action="store_true", help="Poll opt-in settings and publish closed M30 snapshots")
    args = parser.parse_args(argv)
    source = None
    try:
        config = load_config(args.config)
        source = CandleSource(config, importlib.import_module("MetaTrader5"))
        if args.probe:
            source.attach()
            print(json.dumps({"status": "verified", "mode": "read_only", "timeframe": "M30",
                              "accountLast4": config.expected_login[-4:]}), flush=True)
            return 0
        monitor, last_status = Monitor(config, source, Transport(config)), None
        while True:
            delay = 15
            try:
                status = monitor.step()
            except Refusal as error:
                source.close()
                if error.fatal:
                    raise
                status, delay = error.code, 30
            if status != last_status:
                print(json.dumps({"status": status, "mode": "read_only", "timeframe": "M30"}), flush=True)
                last_status = status
            time.sleep(delay)
    except KeyboardInterrupt:
        return 0
    except Refusal as error:
        print(json.dumps({"status": "stopped", "reason": error.code}), flush=True)
        return 1
    except Exception:
        # Never print package exceptions: they may contain credentials or host data.
        print(json.dumps({"status": "stopped", "reason": "monitor_unavailable"}), flush=True)
        return 1
    finally:
        if source is not None:
            source.close()


if __name__ == "__main__":
    raise SystemExit(main())
