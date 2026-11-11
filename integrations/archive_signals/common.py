"""Small validation helpers. Exceptions contain fixed codes, never credentials."""
import json
import re
import time

ENDPOINT = "https://hollowheir.com/_archive/mt5/signals"
BAR_SECONDS = 1800
MAX_BARS = 256
UUID = re.compile(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}", re.I)


class Refusal(Exception):
    def __init__(self, code, fatal=False):
        super().__init__(code)
        self.code, self.fatal = code, fatal


def now_ms():
    return time.time_ns() // 1_000_000


def exact(value, keys):
    if type(value) is not dict or set(value) != set(keys):
        raise Refusal("invalid_fields", True)


def text(value, maximum=128):
    return (isinstance(value, str) and 0 < len(value) <= maximum and value == value.strip()
            and not any(ord(char) < 32 or ord(char) == 127 for char in value))


def gold_symbol(value):
    # The configured identifier is used verbatim; no symbol search or substitution.
    return (text(value, 40) and re.fullmatch(r"(?:GOLD|XAUUSD)(?:(?:[.#_-][a-z0-9#._-]*)|micro|mini|m)?", value, re.I) is not None)


def unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise Refusal("duplicate_json_field", True)
        result[key] = value
    return result


def canonical(value):
    return json.dumps(value, separators=(",", ":"), sort_keys=True, allow_nan=False)
