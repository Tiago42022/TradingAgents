"""Offline tests: no installed MT5 package, terminal, network or order calls."""
import ast
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from .common import ENDPOINT, Refusal, gold_symbol
from .config import Config, load_config, verify_acl, powershell_environment
from .monitor import CandleSource, Monitor, closed_bars, settings
from .transport import LIMIT, Transport
from .cli import main
from . import cli as cli_module, config as config_module

CONNECTION = "29ce2df4-f2ba-4982-9ca5-148f864ef31d"
BASE = 1_800_000_000


def bar(index):
    return {"time": BASE + index * 1800, "open": 4000.123456789, "high": 4003.5,
            "low": 3998.0, "close": 4001.25}


class FakeMT5:
    TIMEFRAME_M30 = 30

    def __init__(self):
        self.calls = []
        self.terminal = NS(path=r"C:\FixtureMT5", data_path=r"C:\FixtureData", connected=True)
        self.account = NS(login=12342660, server="Fixture-MT5")
        self.rates = [bar(i) for i in range(8)]
        self.after_read = lambda: None
        self.found = True

    def initialize(self, *args, **kwargs):
        self.calls.append(("initialize", args, kwargs))
        return True

    def terminal_info(self):
        self.calls.append(("terminal_info",))
        return self.terminal

    def account_info(self):
        self.calls.append(("account_info",))
        return self.account

    def symbol_info(self, symbol):
        self.calls.append(("symbol_info", symbol))
        return NS(name=symbol) if self.found else None

    def copy_rates_from_pos(self, *args):
        self.calls.append(("copy_rates_from_pos", *args))
        self.after_read()
        return self.rates

    def shutdown(self):
        self.calls.append(("shutdown",))


class FakeTransport:
    def __init__(self):
        self.selected = {"schema": 1, "connectionId": CONNECTION, "enabled": True,
                         "symbol": "GOLD", "timeframe": "M30"}
        self.posts, self.gets = [], 0
        self.reject_get = False

    def get(self):
        self.gets += 1
        if self.reject_get:
            raise Refusal("transport_unavailable")
        return deepcopy(self.selected)

    def post(self, payload):
        self.posts.append(deepcopy(payload))
        return {"ok": True}


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.clock = [(BASE + 6 * 1800) * 1000 + 200]
        self.config = Config(r"C:\FixtureMT5\terminal64.exe", r"C:\FixtureData", "12342660",
                             "Fixture-MT5", CONNECTION, "c" * 64)
        self.api, self.transport = FakeMT5(), FakeTransport()
        self.source = CandleSource(self.config, self.api, lambda: self.clock[0], running=lambda _: True)
        self.monitor = Monitor(self.config, self.source, self.transport, lambda: self.clock[0])

    def test_only_closed_m30_raw_bars_are_uploaded(self):
        self.assertEqual(self.monitor.step(), "ready")
        payload = self.transport.posts[-1]
        self.assertEqual(payload["timeframe"], "M30")
        self.assertEqual([b["time"] for b in payload["bars"]], [bar(i)["time"] for i in range(6)])
        self.assertEqual(payload["bars"][0]["open"], repr(4000.123456789))
        self.assertEqual(payload["accountLogin"], "12342660")
        self.assertEqual(payload["capturedAt"], self.clock[0])
        self.assertIn(("copy_rates_from_pos", "GOLD", 30, 0, 257), self.api.calls)
        self.assertEqual(set(payload), {"schema", "connectionId", "accountLogin", "server", "capturedAt",
                                       "sequence", "symbol", "timeframe", "status", "bars"})

    def test_just_before_close_excludes_and_exact_close_includes_bar(self):
        rates = [bar(0)]
        close = (BASE + 1800) * 1000
        self.assertEqual(closed_bars(rates, close - 1), [])
        self.assertEqual(len(closed_bars(rates, close)), 1)

    def test_candle_closing_during_read_is_excluded(self):
        self.clock[0] = (BASE + 6 * 1800) * 1000 - 1
        self.api.after_read = lambda: self.clock.__setitem__(0, self.clock[0] + 10)
        self.monitor.step()
        self.assertEqual(len(self.transport.posts[-1]["bars"]), 5)

    def test_latest_contiguous_segment_after_gap_only(self):
        self.api.rates = [bar(0), bar(1), bar(4), bar(5), bar(6), bar(7)]
        self.clock[0] = (BASE + 8 * 1800) * 1000
        self.monitor.step()
        self.assertEqual([b["time"] for b in self.transport.posts[-1]["bars"]], [bar(i)["time"] for i in range(4, 8)])

    def test_at_most_256_closed_bars(self):
        rates = [bar(i) for i in range(257)]
        actual = closed_bars(rates, (BASE + 300 * 1800) * 1000)
        self.assertEqual(len(actual), 256)
        self.assertEqual(actual[0]["time"], bar(1)["time"])
        with self.assertRaises(Refusal):
            closed_bars(rates + [bar(258)], (BASE + 300 * 1800) * 1000)

    def test_empty_insufficient_and_missing_rates_report_unavailable(self):
        for rates in (None, [], [bar(i) for i in range(3)]):
            self.api.rates = rates
            self.assertEqual(self.monitor.step(), "unavailable")
            self.assertEqual(self.transport.posts[-1]["bars"], [])

    def test_bad_prices_or_times_are_rejected(self):
        mutations = [("open", float("nan")), ("high", float("inf")), ("low", -1.0),
                     ("open", True), ("open", "4000"), ("high", 1.0), ("low", 5000.0),
                     ("time", True), ("time", BASE + 1), ("time", float(BASE))]
        for key, value in mutations:
            with self.subTest(key=key, value=str(value)):
                row = bar(0)
                row[key] = value
                with self.assertRaises(Refusal):
                    closed_bars([row], self.clock[0])

    def test_duplicate_and_reverse_rates_fail_closed(self):
        for rates in ([bar(0), bar(0)], [bar(1), bar(0)]):
            with self.assertRaises(Refusal):
                closed_bars(rates, self.clock[0])

    def test_disabled_does_not_attach_read_candles_or_post(self):
        self.transport.selected.update(enabled=False, symbol=None)
        for _ in range(3):
            self.assertEqual(self.monitor.step(), "disabled")
        self.assertEqual(self.transport.gets, 3)
        self.assertEqual(self.transport.posts, [])
        self.assertEqual(self.api.calls, [])

    def test_disabling_detaches_existing_session(self):
        self.monitor.step()
        self.transport.selected["enabled"] = False
        count = len(self.transport.posts)
        self.monitor.step()
        self.assertEqual(len(self.transport.posts), count)
        self.assertFalse(self.source.attached)
        self.assertEqual(self.api.calls[-1], ("shutdown",))

    def test_settings_errors_do_not_reuse_previous_enable(self):
        self.monitor.step()
        self.transport.reject_get = True
        with self.assertRaises(Refusal):
            self.monitor.step()
        self.assertFalse(self.source.attached)
        self.assertEqual(len(self.transport.posts), 1)

    def test_wrong_settings_binding_timeframe_type_and_symbols_rejected(self):
        for key, value in (("connectionId", "other"), ("timeframe", "M15"), ("schema", True),
                           ("enabled", 1), ("symbol", "EURUSD"), ("symbol", "GOLDEN"), ("symbol", None)):
            selected = dict(self.transport.selected, **{key: value})
            with self.subTest(key=key, value=value), self.assertRaises(Refusal):
                settings(selected, CONNECTION)
        with self.assertRaises(Refusal):
            settings(dict(self.transport.selected, command="anything"), CONNECTION)

    def test_exact_gold_variants_only_no_symbol_guessing(self):
        for symbol in ("GOLD", "GOLD#", "GOLDmicro", "GOLD.m", "XAUUSD", "XAUUSDm", "XAUUSD-mini"):
            self.assertTrue(gold_symbol(symbol))
        self.transport.selected["symbol"] = "XAUUSDm"
        self.monitor.step()
        self.assertIn(("copy_rates_from_pos", "XAUUSDm", 30, 0, 257), self.api.calls)
        self.api.found = False
        previous = sum(call[0] == "copy_rates_from_pos" for call in self.api.calls)
        self.assertEqual(self.monitor.step(), "unavailable")
        self.assertEqual(sum(call[0] == "copy_rates_from_pos" for call in self.api.calls), previous)

    def test_account_server_path_and_disconnection_checked_before_read(self):
        mutations = [("account", "login", 123), ("account", "server", "Wrong-MT5"),
                     ("terminal", "path", r"C:\OtherTerminal"), ("terminal", "data_path", r"C:\OtherData"),
                     ("terminal", "connected", False)]
        for obj, key, value in mutations:
            with self.subTest(key=key):
                self.setUp()
                setattr(getattr(self.api, obj), key, value)
                self.assertEqual(self.monitor.step(), "unavailable")
                self.assertFalse(any(call[0] == "copy_rates_from_pos" for call in self.api.calls))
                self.assertEqual(self.transport.posts[-1]["bars"], [])

    def test_account_switch_during_read_discards_capture_and_recovers(self):
        self.api.after_read = lambda: setattr(self.api.account, "login", 123)
        self.assertEqual(self.monitor.step(), "unavailable")
        self.assertEqual(self.transport.posts[-1]["bars"], [])
        self.assertFalse(self.source.attached)
        self.api.account.login = 12342660
        self.api.after_read = lambda: None
        self.clock[0] += 15000
        self.assertEqual(self.monitor.step(), "ready")

    def test_absent_terminal_is_never_started(self):
        self.source.running = lambda _: False
        self.assertEqual(self.monitor.step(), "unavailable")
        self.assertEqual(self.api.calls, [])

    def test_clock_rollback_and_long_capture_discard_bars(self):
        for change in (-1000, 31000):
            self.setUp()
            self.api.after_read = lambda: self.clock.__setitem__(0, self.clock[0] + change)
            self.assertEqual(self.monitor.step(), "unavailable")
            self.assertEqual(self.transport.posts[-1]["bars"], [])

    def test_identical_polls_and_restart_send_snapshots_not_duplicate_actions(self):
        self.monitor.step()
        first = self.transport.posts[-1]
        self.clock[0] += 15000
        self.monitor.step()
        second = self.transport.posts[-1]
        self.assertEqual(first["bars"], second["bars"])
        self.assertGreater(second["sequence"], first["sequence"])
        self.source.close()
        self.clock[0] += 15000
        replacement = Monitor(self.config, self.source, self.transport, lambda: self.clock[0])
        replacement.step()
        self.assertEqual(first["bars"], self.transport.posts[-1]["bars"])
        self.assertGreater(self.transport.posts[-1]["sequence"], second["sequence"])
        self.assertTrue(all("action" not in item and "signal" not in item for item in self.transport.posts))

    def test_large_wall_clock_rollback_cannot_forge_future_sequence(self):
        self.monitor.step()
        self.clock[0] -= 6000
        with self.assertRaises(Refusal):
            self.monitor.step()
        self.assertEqual(len(self.transport.posts), 1)

    def test_probe_attachment_does_not_need_trading_permissions_or_read_candles(self):
        self.source.attach()
        self.source.close()
        self.assertTrue(all(call[0] in ("initialize", "terminal_info", "account_info", "shutdown") for call in self.api.calls))
        self.assertEqual(self.api.calls[0][2], {"timeout": 10000})

    def test_source_has_only_read_only_mt5_surface_and_no_executor_imports(self):
        directory = Path(__file__).parent
        allowed = {"initialize", "terminal_info", "account_info", "symbol_info", "copy_rates_from_pos", "shutdown", "TIMEFRAME_M30"}
        for file in directory.glob("*.py"):
            if file.name.startswith("test_"):
                continue
            tree = ast.parse(file.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Attribute) and node.value.attr == "api":
                    self.assertIn(node.attr, allowed)
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    imported = node.module if isinstance(node, ast.ImportFrom) else ",".join(alias.name for alias in node.names)
                    self.assertNotIn("archive_mt5", imported or "")
                    self.assertNotIn("executor", imported or "")


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.config = Config("terminal", "data", "1234", "server", CONNECTION, "d" * 64)
        self.calls = []
        self.status, self.body = 200, b'{"ok":true}'
        owner = self

        class Connection:
            def request(self, *args, **kwargs):
                owner.calls.append((args, kwargs))

            def getresponse(self):
                return NS(status=owner.status, read=lambda size: owner.body[:size])

            def close(self):
                pass

        self.transport = Transport(self.config, factory=lambda *args, **kwargs: Connection())

    def test_host_method_path_and_token_are_fixed(self):
        self.transport.get()
        self.assertEqual(self.calls[0][0], ("GET", "/_archive/mt5/signals"))
        self.assertEqual(self.calls[0][1]["headers"]["Authorization"], "Bearer " + self.config.token)
        self.transport.post({"schema": 1})
        self.assertEqual(self.calls[-1][0], ("POST", "/_archive/mt5/signals"))

    def test_redirect_or_auth_failure_is_fatal_without_following(self):
        for status in (301, 302, 307, 401, 403, 404):
            self.status = status
            count = len(self.calls)
            with self.assertRaises(Refusal) as caught:
                self.transport.get()
            self.assertTrue(caught.exception.fatal)
            self.assertEqual(len(self.calls), count + 1)

    def test_snapshot_conflict_is_transient_and_does_not_retry_post(self):
        self.status = 409
        with self.assertRaises(Refusal) as caught:
            self.transport.post({"schema": 1})
        self.assertFalse(caught.exception.fatal)
        self.assertEqual(caught.exception.code, "settings_or_snapshot_changed")
        self.assertEqual(len(self.calls), 1)

    def test_server_bodies_cannot_leak_in_errors(self):
        self.status, self.body = 500, b'credential-looking-server-text'
        with self.assertRaises(Refusal) as caught:
            self.transport.get()
        self.assertEqual(str(caught.exception), "transport_unavailable")

    def test_oversized_duplicate_and_nonobject_response_fail_closed(self):
        for body in (b"x" * (LIMIT + 1), b'{"ok":true,"ok":false}', b"[]"):
            self.body = body
            with self.assertRaises(Refusal):
                self.transport.get()


class ConfigTests(unittest.TestCase):
    def test_acl_verifier_uses_windows_modules_not_inherited_pwsh_modules(self):
        with patch.dict("os.environ", {"PSModulePath": r"C:\FixturePowerShell7\Modules", "SystemRoot": r"C:\Windows"}), \
             patch.object(config_module.subprocess, "run", return_value=NS(returncode=0)) as run:
            verify_acl(r"C:\FixtureProtected\mt5-signals.json")
            passed = run.call_args.kwargs["env"]
            self.assertEqual(passed["PSModulePath"], str(Path(r"C:\Windows") / "System32/WindowsPowerShell/v1.0/Modules"))
            self.assertNotIn("FixturePowerShell7", passed["PSModulePath"])
            self.assertEqual(run.call_args.kwargs["timeout"], 10)

    def test_password_and_execution_fields_are_not_accepted(self):
        with tempfile.TemporaryDirectory() as folder:
            location = Path(folder) / "mt5-signals.json"
            executable = Path(folder) / "terminal64.exe"
            executable.touch()
            data = {"terminalPath": str(executable), "terminalDataPath": folder, "expectedLogin": "12342660",
                    "expectedServer": "Fixture-MT5", "connectionId": CONNECTION, "token": "a" * 64, "endpoint": ENDPOINT}
            with patch.object(config_module, "external_path", return_value=location), patch.object(config_module, "verify_acl"):
                location.write_text(json.dumps(data), encoding="utf-8")
                self.assertEqual(load_config(str(location)).connection_id, CONNECTION)
                for extra in ("password", "manualTrading", "executionToken"):
                    location.write_text(json.dumps(dict(data, **{extra: "not-accepted"})), encoding="utf-8")
                    with self.assertRaises(Refusal):
                        load_config(str(location))

    def test_credentials_do_not_appear_in_config_repr(self):
        token = "a" * 64
        self.assertNotIn(token, repr(Config("exe", "data", "1234", "server", CONNECTION, token)))


class CliTests(unittest.TestCase):
    def test_probe_reads_identity_only_without_network(self):
        config = Config(r"C:\FixtureMT5\terminal64.exe", r"C:\FixtureData", "12342660",
                        "Fixture-MT5", CONNECTION, "e" * 64)
        api = FakeMT5()
        output = io.StringIO()
        with patch.object(cli_module, "load_config", return_value=config), \
             patch.object(cli_module, "CandleSource", side_effect=lambda c, a: CandleSource(c, a, running=lambda _: True)), \
             patch.object(cli_module, "Transport") as transport, \
             patch.object(cli_module, "importlib", NS(import_module=lambda _: api)), redirect_stdout(output):
            self.assertEqual(main(["--config", "external-file", "--probe"]), 0)
        transport.assert_not_called()
        self.assertEqual(json.loads(output.getvalue()), {"status": "verified", "mode": "read_only", "timeframe": "M30", "accountLast4": "2660"})
        self.assertFalse(any(call[0] == "copy_rates_from_pos" for call in api.calls))
        self.assertEqual(api.calls[-1], ("shutdown",))

    def test_unexpected_exception_does_not_disclose_exception_text(self):
        output = io.StringIO()
        with patch.object(cli_module, "load_config", side_effect=RuntimeError("secret-value")), redirect_stdout(output):
            self.assertEqual(main(["--config", "external-file", "--probe"]), 1)
        self.assertNotIn("secret-value", output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["reason"], "monitor_unavailable")


if __name__ == "__main__":
    unittest.main()
