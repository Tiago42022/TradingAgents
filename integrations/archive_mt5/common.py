"""Bounded wire values shared by the optional Archive adapter."""
import hashlib
import json
import re
import time
from decimal import Decimal, InvalidOperation

BASE = "https://hollowheir.com/_archive/mt5/trading"
MARKETS = {"gold", "tech", "oils", "metals", "entertainment", "forex", "crypto", "shares"}
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")
DECIMAL = re.compile(r"-?(?:0|[1-9][0-9]{0,14})(?:\.[0-9]{1,12})?\Z")


class Refusal(Exception):
    """Fixed public code only; never include raw broker/provider exceptions."""

    def __init__(self, code, fatal=False):
        super().__init__(code)
        self.code, self.fatal = code, fatal


def now_ms():
    return time.time_ns() // 1_000_000


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def unique_json(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise Refusal("duplicate_json_key", True)
        value[key] = item
    return value


def exact(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise Refusal("invalid_command", True)


def text(value, maximum=128):
    return isinstance(value, str) and 0 < len(value) <= maximum and value == value.strip() and not re.search(r"[\x00-\x1f\x7f-\x9f<>]", value)


def decimal(value, positive=False, nonnegative=False):
    if not isinstance(value, str) or not DECIMAL.fullmatch(value):
        raise Refusal("invalid_decimal")
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise Refusal("invalid_decimal") from None
    if not result.is_finite() or positive and result <= 0 or nonnegative and result < 0:
        raise Refusal("invalid_decimal")
    return result


def number(value, positive=False, nonnegative=False):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise Refusal("invalid_broker_value")
    result = format(Decimal(str(value)), "f")
    decimal(result, positive=positive, nonnegative=nonnegative)
    return result


def ticket(value):
    if not isinstance(value, int) or isinstance(value, bool) or not 0 < value < 2**64:
        return None
    return str(value)


def outcome(code, timestamp, volume=None, **values):
    messages = {
        "filled": "The broker reported a fill.",
        "partial": "Part of the order filled. No remainder was resubmitted.",
        "accepted": "The broker accepted the order; final execution is pending.",
        "unknown": "Execution outcome is unknown. Do not submit a replacement.",
        "rejected": "The order was not submitted or was rejected by the broker.",
        "trading_permissions_blocked": "MT5 trading permissions are blocked. Check desktop Algo Trading and account access.",
        "manual_execution_disabled": "Manual execution is disabled in this connector's local configuration.",
        "broker_preflight_rejected": "MT5 rejected the order preflight. Check funds and broker restrictions.",
        "insufficient_margin": "MT5 reported insufficient funds for this order.",
        "quote_stale": "A fresh broker quote is unavailable. Review again after the instrument updates.",
        "price_moved": "The quote moved beyond the requested deviation. Review the order again.",
        "review_expired": "The order review expired. Review it again before confirming.",
        "review_changed": "The instrument or account exposure changed. Review the order again.",
        "netting_symbol_not_flat": "This first version requires the selected symbol to be flat on a netting account.",
        "pending_orders_present": "The selected symbol already has pending orders. No new order was submitted.",
    }
    return {"code": code, "message": messages.get(code, "Order checks did not pass. Review again."),
            "retcode": None, "orderTicket": None, "dealTicket": None,
            "requestedVolume": volume, "filledVolume": None, "fillPrice": None,
            "checkedAt": timestamp, **values}
