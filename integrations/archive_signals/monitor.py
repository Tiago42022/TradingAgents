"""Collect observed raw OHLC only. Strategy evaluation belongs to Archive.

There are no order, position, account-login or terminal-configuration operations.
The terminal must already be open and signed in by its operator.
"""
from decimal import Decimal, InvalidOperation
import math
import ntpath
from numbers import Integral, Real

from .common import BAR_SECONDS, MAX_BARS, Refusal, exact, gold_symbol, now_ms
from .config import normalized, terminal_running


def settings(value, connection_id):
    exact(value, ["schema", "connectionId", "enabled", "symbol", "timeframe"])
    if (type(value["schema"]) is not int or value["schema"] != 1
            or value["connectionId"] != connection_id or type(value["enabled"]) is not bool
            or value["timeframe"] != "M30"
            or not (gold_symbol(value["symbol"]) or (not value["enabled"] and value["symbol"] is None))):
        raise Refusal("invalid_settings", True)
    return value


def decimal_price(value):
    """Keep the MT5 binary float's round-trip decimal spelling, not rounded text."""
    if isinstance(value, bool) or not isinstance(value, Real):
        raise Refusal("invalid_candles")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise Refusal("invalid_candles")
    result = repr(numeric)
    try:
        decimal = Decimal(result)
    except InvalidOperation:
        raise Refusal("invalid_candles") from None
    if not Decimal(0) < decimal < Decimal("1e12") or len(result) > 32:
        raise Refusal("invalid_candles")
    return result, decimal


def closed_bars(rates, as_of_ms):
    """Validate and retain the latest contiguous run of at most 256 closed bars.

    A weekend/missing interval resets history; it is never filled synthetically.
    The current MT5 bar (index zero) is filtered by its actual UTC close time.
    """
    if type(as_of_ms) is not int or as_of_ms < 0 or rates is None:
        raise Refusal("invalid_candles")
    try:
        if not 1 <= len(rates) <= MAX_BARS + 1:
            raise Refusal("invalid_candles")
        result, previous = [], None
        for row in rates:
            timestamp = row["time"]
            if isinstance(timestamp, bool) or not isinstance(timestamp, Integral):
                raise Refusal("invalid_candles")
            timestamp = int(timestamp)
            if timestamp < 0 or timestamp % BAR_SECONDS or (previous is not None and timestamp <= previous):
                raise Refusal("invalid_candles")
            previous = timestamp
            if (timestamp + BAR_SECONDS) * 1000 > as_of_ms:
                continue
            item, prices = {"time": timestamp}, {}
            for key in ("open", "high", "low", "close"):
                item[key], prices[key] = decimal_price(row[key])
            if prices["low"] > min(prices["open"], prices["close"]) or prices["high"] < max(prices["open"], prices["close"]):
                raise Refusal("invalid_candles")
            if result and timestamp != result[-1]["time"] + BAR_SECONDS:
                result = []
            result.append(item)
        return result[-MAX_BARS:]
    except Refusal:
        raise
    except Exception:
        raise Refusal("invalid_candles") from None


class CandleSource:
    def __init__(self, config, api, clock=now_ms, running=terminal_running):
        self.config, self.api, self.clock, self.running = config, api, clock, running
        self.attached = False

    def identity(self):
        try:
            terminal, account = self.api.terminal_info(), self.api.account_info()
            if terminal is None or account is None or terminal.connected is not True:
                raise Refusal("terminal_unavailable")
            if (normalized(terminal.path) != normalized(ntpath.dirname(self.config.terminal_path))
                    or normalized(terminal.data_path) != normalized(self.config.terminal_data_path)
                    or str(account.login) != self.config.expected_login or account.server != self.config.expected_server):
                raise Refusal("terminal_binding_mismatch")
        except Refusal:
            raise
        except Exception:
            raise Refusal("terminal_unavailable") from None

    def attach(self):
        if self.attached:
            self.identity()
            return
        if not self.running(self.config.terminal_path):
            raise Refusal("terminal_not_running")
        try:
            if self.api.initialize(self.config.terminal_path, timeout=10000) is not True:
                raise Refusal("terminal_unavailable")
            self.attached = True
            self.identity()
        except Refusal:
            self.close()
            raise
        except Exception:
            self.close()
            raise Refusal("terminal_unavailable") from None

    def capture(self, symbol):
        if not gold_symbol(symbol):
            raise Refusal("invalid_symbol", True)
        try:
            self.attach()
            self.identity()
            before = self.clock()
            info = self.api.symbol_info(symbol)
            if info is None or info.name != symbol:
                raise Refusal("symbol_unavailable")
            # No symbol_select call: this adapter never changes Market Watch.
            rates = self.api.copy_rates_from_pos(symbol, self.api.TIMEFRAME_M30, 0, MAX_BARS + 1)
            self.identity()  # Discard the whole capture if the user changed accounts.
            after = self.clock()
            if type(before) is not int or type(after) is not int or after < before or after - before > 30000:
                raise Refusal("capture_clock_changed")
            # Use the earlier timestamp so an interval closing during the read
            # cannot be mistaken for a fully collected, closed candle.
            return closed_bars(rates, before), after
        except Refusal:
            self.close()
            raise
        except Exception:
            self.close()
            raise Refusal("candles_unavailable") from None

    def close(self):
        if self.attached:
            try:
                self.api.shutdown()
            except Exception:
                pass
            finally:
                self.attached = False


class Monitor:
    def __init__(self, config, source, transport, clock=now_ms):
        self.config, self.source, self.transport, self.clock = config, source, transport, clock
        self.last_sequence = -1

    def step(self):
        try:
            selected = settings(self.transport.get(), self.config.connection_id)
        except Exception:
            self.source.close()
            raise
        if not selected["enabled"]:
            self.source.close()
            return "disabled"
        status, bars, captured = "ready", [], self.clock()
        try:
            bars, captured = self.source.capture(selected["symbol"])
            if len(bars) < 4:
                status, bars = "unavailable", []
        except Refusal:
            status, bars, captured = "unavailable", [], self.clock()
        if type(captured) is not int or captured < 0:
            raise Refusal("invalid_clock")
        self.last_sequence = max(captured, self.last_sequence + 1)
        if self.last_sequence > captured + 5000 or self.last_sequence > 9007199254740991:
            raise Refusal("capture_clock_changed")
        self.transport.post({
            "schema": 1, "connectionId": self.config.connection_id,
            "accountLogin": self.config.expected_login, "server": self.config.expected_server,
            "capturedAt": captured, "sequence": self.last_sequence,
            "symbol": selected["symbol"], "timeframe": "M30", "status": status, "bars": bars,
        })
        return status
