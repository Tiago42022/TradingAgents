"""Offline tests: FakeMT5 is the only broker. No installed MT5 import occurs."""
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace as NS
import unittest
import uuid

from .broker import Broker
from .common import Refusal, digest
from .config import Config
from .executor import Executor
from .journal import Journal, Singleton
from .transport import Transport

CONNECTION = "11111111-1111-4111-8111-111111111111"


class FakeMT5:
    ACCOUNT_TRADE_MODE_REAL = 2
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_IOC = 1
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    TRADE_ACTION_DEAL = 1
    ORDER_STATE_FILLED = 4
    ORDER_STATE_CANCELED = 2
    ORDER_STATE_EXPIRED = 6
    ORDER_STATE_REJECTED = 5

    def __init__(self, clock):
        self.clock = clock
        self.account = NS(login=12342660, server="Fixture-MT5", trade_mode=2, currency="USD",
                          margin_mode=2, trade_allowed=True, trade_expert=True)
        self.terminal = NS(path=r"C:\MT5", data_path=r"C:\TerminalData", connected=True,
                           trade_allowed=True, tradeapi_disabled=False)
        self.info = NS(name="EURUSD#", description="Euro / US dollar", path="Forex\\Majors", digits=5,
                       point=.00001, trade_tick_size=.00001, volume_min=.01, volume_max=20., volume_step=.01,
                       volume_limit=100., trade_mode=4, order_mode=1 | 16 | 32, trade_stops_level=10,
                       trade_freeze_level=0, trade_exemode=2, filling_mode=3, trade_contract_size=100000.)
        self.tick = NS(time_msc=clock[0], bid=1.1, ask=1.10002)
        self.positions = []
        self.active = []
        self.history_orders = []
        self.history_deals = []
        self.sent = []
        self.checks = []
        self.response = NS(retcode=10009, order=500, deal=501, volume=.1, price=1.10003)
        self.checkcode = 0
        self.send_hook = None
        self.check_hook = None

    def initialize(self, *args, **kwargs):
        return True

    def terminal_info(self):
        return self.terminal

    def account_info(self):
        return self.account

    def symbols_get(self):
        return [self.info]

    def symbol_info(self, symbol):
        return self.info if symbol == self.info.name else None

    def symbol_info_tick(self, symbol):
        return self.tick

    def symbol_select(self, symbol, visible):
        self.info.visible = visible
        return symbol == self.info.name

    def positions_get(self, **kwargs):
        return self.positions

    def orders_get(self, **kwargs):
        return self.active

    def order_calc_margin(self, *args):
        return 20.

    def order_check(self, request):
        self.checks.append(dict(request))
        if self.check_hook:
            self.check_hook()
        return NS(retcode=self.checkcode)

    def order_send(self, request):
        self.sent.append(dict(request))
        if self.send_hook:
            return self.send_hook()
        return self.response

    def history_orders_get(self, *args):
        return self.history_orders

    def history_deals_get(self, *args):
        return self.history_deals

    def shutdown(self):
        pass


class FakeTransport:
    def __init__(self, clock):
        self.clock, self.requests, self.next_command = clock, [], None
        self.fail = False

    def post(self, action, payload):
        self.requests.append((action, payload))
        if self.fail:
            raise Refusal("transport_unavailable")
        if action == "poll":
            return {"command": self.next_command, "serverTime": self.clock[0]}
        return {"accepted": True, "id": payload["id"], "status": payload["status"]}


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.clock = [1900000000000]
        self.config = Config(r"C:\MT5\terminal64.exe", r"C:\TerminalData", "12342660", "Fixture-MT5", CONNECTION,
                             "ab" * 32, self.directory.name, True)
        self.api = FakeMT5(self.clock)
        self.broker = Broker(self.config, self.api, lambda: self.clock[0])
        self.journal = Journal(self.directory.name)
        self.addCleanup(self.journal.close)
        self.transport = FakeTransport(self.clock)
        self.executor = Executor(self.config, self.broker, self.journal, self.transport)
        self.intent = str(uuid.uuid4())
        self.input = {"market": "forex", "symbol": "EURUSD#", "side": "buy", "volume": "0.1",
                      "stopLoss": "1.09", "takeProfit": "1.12", "deviationPoints": 10}

    def prepare(self, value=None):
        command = {"id": self.intent, "claimId": str(uuid.uuid4()), "action": "prepare",
                   "expiresAt": self.clock[0] + 60000, "input": value or self.input}
        self.executor.command(command)
        self.executor.flush()
        row = self.journal.get(self.intent)
        return json.loads(row["prepared"]) if row else None

    def execute_command(self, prepared):
        return {"id": self.intent, "claimId": str(uuid.uuid4()), "action": "execute",
                "expiresAt": self.clock[0] + 9000, "reviewHash": prepared["reviewHash"]}

    def test_preflight_is_read_only_and_review_hash_is_canonical(self):
        prepared = self.prepare()
        self.assertEqual(self.api.sent, [])
        self.assertEqual(prepared["reviewHash"], digest(prepared["review"]))
        self.assertEqual(prepared["review"]["price"], prepared["review"]["ask"])
        self.assertEqual(prepared["request"]["type_filling"], 0)
        self.assertNotIn("price", prepared["request"])
        self.assertEqual(self.journal.db.execute("PRAGMA synchronous").fetchone()[0], 2)

    def test_only_explicit_broker_symbol_is_subscribed(self):
        self.api.info.visible = False
        self.broker.prepare(self.intent, self.input)
        self.assertTrue(self.api.info.visible)
        with self.assertRaises(Refusal):
            self.broker.prepare(self.intent, {**self.input, "symbol": "OANDA:EURUSD"})
        self.assertEqual(self.api.sent, [])

    def test_exact_confirmed_command_sends_once_and_replayed_command_does_not(self):
        prepared = self.prepare()
        command = self.execute_command(prepared)
        self.executor.command(command)
        self.executor.command(command)
        self.assertEqual(len(self.api.sent), 1)
        self.assertEqual(self.journal.get(self.intent)["state"], "filled")

    def test_no_local_review_or_wrong_hash_cannot_send(self):
        with self.assertRaises(Refusal):
            self.executor.command({"id": self.intent, "claimId": str(uuid.uuid4()), "action": "execute", "expiresAt": self.clock[0]+9000, "reviewHash": "a"*64})
        prepared = self.prepare()
        command = self.execute_command(prepared); command["reviewHash"] = "b"*64
        self.executor.command(command)
        self.assertEqual(self.api.sent, [])

    def test_local_request_corruption_is_detected_before_send(self):
        prepared = self.prepare()
        changed = dict(prepared); changed["request"] = {**prepared["request"], "volume": 10.}
        with self.journal.db:
            self.journal.db.execute("UPDATE intents SET prepared=? WHERE id=?", (json.dumps(changed), self.intent))
        with self.assertRaises(Refusal):
            self.executor.command(self.execute_command(prepared))
        self.assertEqual(self.api.sent, [])

    def test_unknown_remote_fields_and_arbitrary_action_rejected(self):
        for command in ({"action": "order_send"}, {"id": self.intent, "claimId": str(uuid.uuid4()), "action": "catalog", "expiresAt": self.clock[0]+1000, "shell": "no"}):
            with self.assertRaises(Refusal):
                self.executor.command(command)
        self.assertEqual(self.api.sent, [])

    def test_account_data_path_mode_and_permissions_fail_closed(self):
        for obj, field, value in ((self.api.account, "login", 99), (self.api.account, "server", "Other"),
                                  (self.api.account, "trade_mode", 0), (self.api.terminal, "data_path", "Other"),
                                  (self.api.terminal, "connected", False), (self.api.terminal, "trade_allowed", False),
                                  (self.api.terminal, "tradeapi_disabled", True), (self.api.account, "trade_expert", False)):
            previous = getattr(obj, field); setattr(obj, field, value)
            with self.assertRaises(Refusal):
                self.broker.prepare(self.intent, self.input)
            setattr(obj, field, previous)

    def test_invalid_volume_precision_stops_and_fill_modes(self):
        for change in ({"volume": "0.001"}, {"volume": "NaN"}, {"volume": "0.11e0"},
                       {"stopLoss": "1.090001"}, {"stopLoss": "1.10001"}, {"takeProfit": "1.09"}):
            with self.assertRaises(Refusal):
                self.broker.prepare(self.intent, {**self.input, **change})
        self.api.info.filling_mode = 2
        prepared = self.broker.prepare(self.intent, self.input)
        self.assertEqual(prepared["review"]["fillingPolicy"], "IOC")
        self.assertEqual(prepared["request"]["type_filling"], 1)
        self.api.info.filling_mode = 0
        with self.assertRaises(Refusal):
            self.broker.prepare(self.intent, self.input)
        self.api.info.trade_exemode = 1
        self.assertEqual(self.broker.prepare(self.intent, self.input)["request"]["type_filling"], 0)

    def test_stale_quote_and_expired_review_never_send(self):
        self.api.tick.time_msc -= 10001
        with self.assertRaises(Refusal):
            self.broker.prepare(self.intent, self.input)
        self.api.tick.time_msc = self.clock[0]
        prepared = self.prepare()
        command = self.execute_command(prepared)
        self.clock[0] += 10000
        self.executor.command(command)
        self.assertEqual(self.api.sent, [])

    def test_preflight_expiring_mid_broker_check_never_sends(self):
        prepared = self.prepare()
        command = self.execute_command(prepared)
        self.api.check_hook = lambda: self.clock.__setitem__(0, self.clock[0] + 10000)
        self.executor.command(command)
        self.assertEqual(self.api.sent, [])

    def test_monotonic_deadline_survives_wall_clock_rollback(self):
        prepared = self.prepare(); command = self.execute_command(prepared)
        elapsed = [0.]
        self.executor.monotonic = lambda: elapsed[0]
        def clock_changed():
            elapsed[0] += 10.
            self.clock[0] -= 1000
        self.api.check_hook = clock_changed
        self.executor.command(command)
        self.assertEqual(self.api.sent, [])

    def position(self):
        return NS(ticket=100, identifier=100, symbol="EURUSD#", type=0, volume=.1, price_open=1.1, sl=1., tp=1.2, time_update_msc=self.clock[0])

    def test_netting_and_changed_exposure_and_pending_orders_rejected(self):
        self.api.account.margin_mode = 0; self.api.positions = [self.position()]
        with self.assertRaises(Refusal):
            self.broker.prepare(self.intent, self.input)
        self.api.account.margin_mode = 2; self.api.positions = []
        prepared = self.prepare(); self.api.positions = [self.position()]
        self.executor.command(self.execute_command(prepared))
        self.assertEqual(self.api.sent, [])
        self.api.active = [NS(ticket=4)]
        with self.assertRaises(Refusal):
            self.broker.prepare(str(uuid.uuid4()), self.input)

    def test_price_spec_and_account_switch_after_review_never_send(self):
        prepared = self.prepare()
        self.api.tick.ask += .1
        self.executor.command(self.execute_command(prepared))
        self.assertEqual(self.api.sent, [])

    def test_journal_failure_is_before_order_send(self):
        prepared = self.prepare()
        self.journal.reserve = lambda *args: (_ for _ in ()).throw(sqlite3.OperationalError("disk full"))
        with self.assertRaises(sqlite3.OperationalError):
            self.executor.command(self.execute_command(prepared))
        self.assertEqual(self.api.sent, [])

    def test_exposure_change_during_reservation_is_checked_before_send(self):
        prepared = self.prepare()
        original = self.journal.reserve
        def changed(*args):
            original(*args)
            self.api.positions = [self.position()]
        self.journal.reserve = changed
        self.executor.command(self.execute_command(prepared))
        self.assertEqual(self.api.sent, [])
        self.assertEqual(self.journal.get(self.intent)["state"], "rejected")

    def test_crash_after_reservation_recovers_unknown_never_resends(self):
        prepared = self.prepare(); command = self.execute_command(prepared)
        self.journal.reserve(self.intent, self.executor.binding, command["claimId"], self.clock[0])
        self.executor.recover()
        self.executor.command(command)
        self.assertEqual(self.journal.get(self.intent)["state"], "unknown")
        self.assertEqual(self.api.sent, [])

    def test_unknown_result_and_lost_ack_do_not_repeat_order(self):
        prepared = self.prepare(); command = self.execute_command(prepared)
        self.api.response = None
        self.executor.command(command)
        self.transport.fail = True
        with self.assertRaises(Refusal):
            self.executor.flush()
        self.transport.fail = False
        self.executor.flush(); self.executor.command(command)
        self.assertEqual(len(self.api.sent), 1)
        self.assertEqual(self.journal.get(self.intent)["state"], "unknown")

    def test_partial_and_placed_and_timeout_are_distinct(self):
        for code, volume, status in ((10010, .04, "partial"), (10008, 0., "accepted"), (10012, 0., "unknown")):
            result = NS(retcode=code, order=500, deal=501, volume=volume, price=1.1)
            self.assertEqual(self.executor.normalize_result(result, "0.1")[0], status)

    def test_contradictory_rejected_fill_remains_unknown(self):
        for value in (.1, float("nan"), -1.):
            result = NS(retcode=10006, order=0, deal=0, volume=value, price=0.)
            self.assertEqual(self.executor.normalize_result(result, "0.1")[0], "unknown")

    def test_catalog_odd_description_and_path_cannot_poison_outbox(self):
        self.api.info.description = "<bad>"
        self.api.info.path = "bad\x7fpath"
        result = self.broker.catalog()[0]
        self.assertEqual(result["description"], self.api.info.name)
        self.assertEqual(result["path"], "")

    def test_history_positive_match_reconciles_partial_no_resend(self):
        prepared = self.prepare(); command = self.execute_command(prepared)
        self.api.response = None; self.executor.command(command); self.executor.flush()
        request = prepared["request"]
        self.api.history_orders = [NS(ticket=500, magic=request["magic"], symbol="EURUSD#", type=0,
                                      volume_initial=.1, volume_current=.06, state=2, time_setup_msc=self.clock[0])]
        self.api.history_deals = [NS(ticket=501, order=500, magic=request["magic"], symbol="EURUSD#", type=0,
                                     volume=.04, price=1.10003, time_msc=self.clock[0])]
        self.executor.reconcile()
        self.assertEqual(self.journal.get(self.intent)["state"], "partial")
        self.assertEqual(len(self.api.sent), 1)

    def test_lagging_deal_history_and_nonfinal_order_do_not_release_queue(self):
        prepared = self.prepare(); command = self.execute_command(prepared)
        self.api.response = None; self.executor.command(command); self.executor.flush()
        request = prepared["request"]
        order = NS(ticket=500, magic=request["magic"], symbol="EURUSD#", type=0,
                   volume_initial=.1, volume_current=0., state=4, time_setup_msc=self.clock[0])
        self.api.history_orders = [order]
        self.api.history_deals = [NS(ticket=501, order=500, magic=request["magic"], symbol="EURUSD#", type=0,
                                     volume=.04, price=1.10003, time_msc=self.clock[0])]
        self.executor.reconcile()
        self.assertEqual(self.journal.get(self.intent)["state"], "unknown")
        order.state = 3; order.volume_current = .06
        self.executor.reconcile()
        self.assertEqual(self.journal.get(self.intent)["state"], "unknown")
        self.assertEqual(len(self.api.sent), 1)

    def test_known_accepted_order_ticket_prevents_other_order_matching(self):
        prepared = self.prepare(); command = self.execute_command(prepared)
        self.api.response = NS(retcode=10008, order=500, deal=0, volume=0., price=0.)
        self.executor.command(command); self.executor.flush()
        request = prepared["request"]
        self.api.history_orders = [NS(ticket=999, magic=request["magic"], symbol="EURUSD#", type=0,
                                      volume_initial=.1, volume_current=0., state=4, time_setup_msc=self.clock[0])]
        self.api.history_deals = [NS(ticket=998, order=999, magic=request["magic"], symbol="EURUSD#", type=0,
                                     volume=.1, price=1.10003, time_msc=self.clock[0])]
        self.executor.reconcile()
        self.assertEqual(self.journal.get(self.intent)["state"], "accepted")

    def test_discarded_preparation_ack_clears_cancelled_local_review(self):
        command = {"id": self.intent, "claimId": str(uuid.uuid4()), "action": "prepare", "expiresAt": self.clock[0]+60000, "input": self.input}
        self.executor.command(command)
        self.transport.post = lambda action, payload: {"accepted": True, "id": payload["id"], "status": payload["status"], "discarded": "cancelled"}
        self.executor.flush()
        self.assertEqual(self.journal.get(self.intent)["state"], "cancelled")
        self.assertEqual(self.journal.pending(), [])
        self.assertEqual(self.api.sent, [])

    def test_no_history_or_wrong_magic_does_not_prove_no_execution(self):
        prepared = self.prepare(); command = self.execute_command(prepared)
        self.api.response = None; self.executor.command(command); self.executor.flush()
        self.executor.reconcile()
        self.assertEqual(self.journal.get(self.intent)["state"], "unknown")
        self.assertEqual(len(self.api.sent), 1)

    def test_singleton_and_connection_binding(self):
        lock = Singleton(self.directory.name)
        try:
            with self.assertRaises(Refusal):
                Singleton(self.directory.name)
        finally:
            lock.close()
        second = Singleton(self.directory.name); second.close()
        with self.assertRaises(Refusal):
            self.journal.bind("wrong-account")

    def test_transport_rejects_redirect_and_does_not_log_server_body(self):
        class Connection:
            def request(self, *args, **kwargs):
                pass
            def getresponse(self):
                return NS(status=302)
            def close(self):
                pass
        transport = Transport(self.config, factory=lambda *args, **kwargs: Connection())
        with self.assertRaisesRegex(Refusal, "device_authorization_or_state_rejected"):
            transport.post("poll", {})
        with self.assertRaises(Refusal):
            transport.post("http://other", {})

    def test_catalog_works_with_trading_disabled_and_has_no_order_calls(self):
        self.api.terminal.trade_allowed = False
        self.assertEqual(self.broker.catalog()[0]["symbol"], "EURUSD#")
        self.assertEqual(self.api.sent, [])


if __name__ == "__main__":
    unittest.main()
