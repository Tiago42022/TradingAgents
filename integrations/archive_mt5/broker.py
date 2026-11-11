"""Exact-account reads and deterministic market-order preflight.

All broker calls are supplied by an adapter. Nothing here logs in, changes
settings, starts strategies, or submits an order during preflight.
"""
import hashlib
import ntpath
import re

from .common import MARKETS, Refusal, canonical, decimal, digest, exact, now_ms, number, text
from .config import normalized, terminal_running

INPUT_FIELDS = ("market", "symbol", "side", "volume", "stopLoss", "takeProfit", "deviationPoints")
SPEC_FIELDS = ("digits", "point", "trade_tick_size", "volume_min", "volume_max", "volume_step", "volume_limit",
               "trade_mode", "order_mode", "trade_stops_level", "trade_freeze_level", "trade_exemode", "filling_mode", "trade_contract_size")
MODES = {0: "request", 1: "instant", 2: "market", 3: "exchange"}


class Broker:
    def __init__(self, config, api, clock=now_ms):
        self.config, self.api, self.clock = config, api, clock
        self.initialized = False

    def initialize(self, running=terminal_running):
        if not running(self.config.terminal_path):
            raise Refusal("terminal_not_running", True)
        if self.api.initialize(self.config.terminal_path, timeout=10000) is not True:
            raise Refusal("terminal_initialization_failed", True)
        self.initialized = True
        self.identity()

    def identity(self):
        terminal, account = self.api.terminal_info(), self.api.account_info()
        if terminal is None or account is None or terminal.connected is not True:
            raise Refusal("terminal_unavailable")
        if (normalized(terminal.path) != normalized(ntpath.dirname(self.config.terminal_path))
                or normalized(terminal.data_path) != normalized(self.config.terminal_data_path)
                or str(account.login) != self.config.expected_login or account.server != self.config.expected_server
                or account.trade_mode != self.api.ACCOUNT_TRADE_MODE_REAL):
            raise Refusal("account_identity_changed", True)
        if not isinstance(account.currency, str) or not re.fullmatch(r"[A-Z][A-Z0-9]{2,11}", account.currency):
            raise Refusal("invalid_currency", True)
        permissions = {"terminalTrading": terminal.trade_allowed is True,
                       "pythonTrading": terminal.tradeapi_disabled is False,
                       "accountTrading": account.trade_allowed is True and account.trade_expert is True}
        return account, permissions

    def catalog(self):
        before, _ = self.identity()
        instruments = self.api.symbols_get()
        if instruments is None or len(instruments) > 20000:
            raise Refusal("catalog_unavailable")
        result, seen = [], set()
        for item in instruments:
            if not text(item.name, 80) or item.name in seen:
                continue
            if getattr(item, "trade_mode", 0) not in (1, 2, 4) or not getattr(item, "order_mode", 0) & 1:
                continue
            # Preserve exact broker symbol; descriptive metadata is bounded.
            description = getattr(item, "description", item.name)
            path = getattr(item, "path", "")
            if not text(description, 160):
                description = item.name
            if not text(path, 200):
                path = ""
            result.append({"symbol": item.name, "description": description, "path": path})
            seen.add(item.name)
        if len(result) > 5000 or len(canonical(result).encode("utf-8")) > 900000:
            raise Refusal("catalog_too_large")
        after, _ = self.identity()
        if before.currency != after.currency:
            raise Refusal("account_identity_changed", True)
        return sorted(result, key=lambda value: value["symbol"])

    def capture(self, symbol):
        account, permissions = self.identity()
        if not all(permissions.values()):
            raise Refusal("trading_permissions_blocked")
        info = self.api.symbol_info(symbol)
        if info is not None and getattr(info, "visible", True) is False:
            # Subscribe only the exact broker symbol explicitly chosen by the
            # user. Market Watch visibility is not permission to place an order.
            if self.api.symbol_select(symbol, True) is not True:
                raise Refusal("instrument_unavailable")
            info = self.api.symbol_info(symbol)
        tick = self.api.symbol_info_tick(symbol)
        positions = self.api.positions_get(symbol=symbol)
        orders = self.api.orders_get(symbol=symbol)
        if info is None or tick is None or positions is None or orders is None or getattr(info, "name", None) != symbol:
            raise Refusal("instrument_unavailable")
        if len(positions) > 200 or len(orders) > 200:
            raise Refusal("exposure_unavailable")
        if orders:
            raise Refusal("pending_orders_present")
        captured = self.clock()
        if type(tick.time_msc) is not int or not captured - 10000 <= tick.time_msc <= captured + 2000:
            raise Refusal("quote_stale")
        bid, ask = number(tick.bid, positive=True), number(tick.ask, positive=True)
        if decimal(ask) < decimal(bid):
            raise Refusal("invalid_quote")
        if account.margin_mode not in (0, 1, 2):
            raise Refusal("unsupported_margin_mode")
        hedging = account.margin_mode == 2
        if positions and not hedging:
            raise Refusal("netting_symbol_not_flat")
        spec = {key: number(getattr(info, key)) for key in SPEC_FIELDS}
        exposure = []
        for p in positions:
            if p.symbol != symbol or p.type not in (0, 1):
                raise Refusal("exposure_unavailable")
            exposure.append({"ticket": str(p.ticket), "identifier": str(p.identifier), "side": p.type,
                             "volume": number(p.volume, positive=True), "price": number(p.price_open),
                             "sl": number(p.sl), "tp": number(p.tp), "updated": p.time_update_msc})
        exposure.sort(key=lambda value: value["ticket"])
        after, _ = self.identity()
        if account.currency != after.currency or account.margin_mode != after.margin_mode:
            raise Refusal("account_identity_changed", True)
        return {"account": account, "info": info, "spec": spec, "exposure": exposure,
                "hedging": hedging, "tick": tick, "bid": bid, "ask": ask}

    def validate_input(self, value):
        exact(value, INPUT_FIELDS)
        if value["market"] not in MARKETS or not text(value["symbol"], 80) or value["side"] not in ("buy", "sell"):
            raise Refusal("invalid_order_input")
        decimal(value["volume"], positive=True)
        for name in ("stopLoss", "takeProfit"):
            if value[name] is not None:
                decimal(value[name], positive=True)
        if type(value["deviationPoints"]) is not int or not 0 <= value["deviationPoints"] <= 1000:
            raise Refusal("invalid_deviation")

    def terms(self, value, data):
        info = data["info"]
        buy = value["side"] == "buy"
        if info.trade_mode not in ((1, 4) if buy else (2, 4)) or not info.order_mode & 1:
            raise Refusal("direction_not_allowed")
        volume = decimal(value["volume"])
        minimum, maximum, step = (decimal(number(getattr(info, key), positive=True)) for key in ("volume_min", "volume_max", "volume_step"))
        if minimum > maximum or not minimum <= volume <= maximum or volume % step != 0:
            raise Refusal("invalid_volume_step")
        same_side = sum((decimal(p["volume"]) for p in data["exposure"] if p["side"] == (0 if buy else 1)), start=decimal("0"))
        limit = decimal(number(info.volume_limit, nonnegative=True))
        if limit and same_side + volume > limit:
            raise Refusal("volume_limit_exceeded")
        point = decimal(number(info.point, positive=True))
        tick_size = decimal(number(info.trade_tick_size, positive=True))
        stops = decimal(number(info.trade_stops_level, nonnegative=True)) * point
        reference = decimal(data["bid"] if buy else data["ask"])
        for name, flag in (("stopLoss", 16), ("takeProfit", 32)):
            price = value[name]
            if price is None:
                continue
            price = decimal(price, positive=True)
            if not info.order_mode & flag or price % tick_size != 0:
                raise Refusal("invalid_protection_price")
            below = buy if name == "stopLoss" else not buy
            if below and (price >= reference or reference - price < stops) or not below and (price <= reference or price - reference < stops):
                raise Refusal("invalid_protection_distance")
        if info.trade_exemode not in MODES:
            raise Refusal("unsupported_execution_mode")
        # SYMBOL_FILLING_FOK=1/IOC=2 are flags; ORDER_FILLING_FOK=0/IOC=1
        # are request enums. RETURN can leave a residual order and is excluded.
        if info.trade_exemode in (0, 1) or info.filling_mode & 1:
            fill, enum = "FOK", self.api.ORDER_FILLING_FOK
        elif info.filling_mode & 2:
            fill, enum = "IOC", self.api.ORDER_FILLING_IOC
        else:
            raise Refusal("unsupported_filling_policy")
        return fill, enum

    def preflight(self, request, data):
        margin = self.api.order_calc_margin(request["type"], request["symbol"], request["volume"], float(data["ask"] if request["type"] == self.api.ORDER_TYPE_BUY else data["bid"]))
        if margin is None:
            raise Refusal("margin_unavailable")
        margin = number(margin, nonnegative=True)
        checked = self.api.order_check(request)
        if checked is None or type(checked.retcode) is not int or checked.retcode != 0:
            if checked is not None and getattr(checked, "retcode", None) == 10019:
                raise Refusal("insufficient_margin")
            raise Refusal("broker_preflight_rejected")
        self.identity()
        return margin

    def prepare(self, identity, value):
        self.validate_input(value)
        data = self.capture(value["symbol"])
        fill, enum = self.terms(value, data)
        price = data["ask"] if value["side"] == "buy" else data["bid"]
        magic = int(hashlib.sha256((self.config.connection_id + identity).encode()).hexdigest()[:15], 16)
        request = {"action": self.api.TRADE_ACTION_DEAL, "symbol": value["symbol"], "volume": float(value["volume"]),
                   "type": self.api.ORDER_TYPE_BUY if value["side"] == "buy" else self.api.ORDER_TYPE_SELL,
                   "deviation": value["deviationPoints"], "type_filling": enum, "magic": magic,
                   "comment": "A" + identity.replace("-", "")[:24]}
        if data["info"].trade_exemode != 2:
            request["price"] = float(price)
        for name, field in (("stopLoss", "sl"), ("takeProfit", "tp")):
            if value[name] is not None:
                request[field] = float(value[name])
        margin = self.preflight(request, data)
        latest = self.capture(value["symbol"])
        if latest["spec"] != data["spec"] or latest["exposure"] != data["exposure"]:
            raise Refusal("preflight_changed")
        prepared_at = self.clock()
        if prepared_at - data["tick"].time_msc > 10000:
            raise Refusal("quote_stale")
        review = {"id": identity, "connectionId": self.config.connection_id,
                  "accountLast4": self.config.expected_login[-4:], "server": self.config.expected_server,
                  "currency": data["account"].currency, **value,
                  "description": data["info"].description if text(data["info"].description, 160) else value["symbol"],
                  "executionMode": MODES[data["info"].trade_exemode], "fillingPolicy": fill,
                  "bid": data["bid"], "ask": data["ask"], "price": price, "point": number(data["info"].point),
                  "volumeMin": number(data["info"].volume_min), "volumeMax": number(data["info"].volume_max),
                  "volumeStep": number(data["info"].volume_step), "estimatedMargin": margin,
                  "quoteAt": data["tick"].time_msc, "preparedAt": prepared_at, "expiresAt": prepared_at + 30000,
                  "hedging": data["hedging"], "existingPositions": len(data["exposure"])}
        prepared = {"review": review, "reviewHash": digest(review), "request": request,
                    "spec": data["spec"], "exposure": data["exposure"], "input": value}
        return {**prepared, "localHash": digest(prepared)}

    def validate_execution(self, prepared, expires_at):
        if not self.config.manual_trading:
            raise Refusal("manual_execution_disabled")
        exact(prepared, ["review", "reviewHash", "request", "spec", "exposure", "input", "localHash"])
        if prepared["localHash"] != digest({k: v for k, v in prepared.items() if k != "localHash"}):
            raise Refusal("local_review_corrupt", True)
        review = prepared["review"]
        self.validate_input(prepared["input"])
        if digest(review) != prepared["reviewHash"] or self.clock() >= min(expires_at, review["expiresAt"]):
            raise Refusal("review_expired")
        data = self.capture(review["symbol"])
        fill, _ = self.terms(prepared["input"], data)
        if data["spec"] != prepared["spec"] or data["exposure"] != prepared["exposure"] or data["hedging"] != review["hedging"] or data["account"].currency != review["currency"] or fill != review["fillingPolicy"]:
            raise Refusal("review_changed")
        price = decimal(data["ask"] if review["side"] == "buy" else data["bid"])
        if abs(price - decimal(review["price"])) > review["deviationPoints"] * decimal(review["point"]):
            raise Refusal("price_moved")
        return data

    def check_execution(self, prepared, expires_at):
        data = self.validate_execution(prepared, expires_at)
        self.preflight(prepared["request"], data)
        # A slow broker check must not preserve an expired review/exposure.
        self.validate_execution(prepared, expires_at)

    def shutdown(self):
        if self.initialized:
            self.initialized = False
            self.api.shutdown()
