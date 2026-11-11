"""Pure, closed-M30 Heiken Ashi signal research for a supplied GOLD symbol.

This module emits observations, not executable orders or simulated fills.
No account, money, sizing, SL/TP, broker, filesystem or network operations exist.
"""

from collections.abc import Iterable
from dataclasses import dataclass, replace
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation, localcontext
from typing import Literal

BAR_SECONDS = 1800
TIMEFRAME = "M30"


def _time(value: int) -> None:
    if type(value) is not int or value < 0:
        raise ValueError("Use nonnegative integer UTC epoch seconds.")


def _timeframe(value: str) -> None:
    if value != TIMEFRAME:
        raise ValueError("This prototype supports M30 only.")


def _price(value: Decimal | str | int) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (Decimal, str, int)):
        raise ValueError("Prices must be Decimal, decimal strings or integers; floats are not accepted.")
    try:
        result = Decimal(value)
    except InvalidOperation as error:
        raise ValueError("Invalid price.") from error
    if not result.is_finite() or result <= 0:
        raise ValueError("Prices must be finite and positive.")
    return result


@dataclass(frozen=True)
class _Bar:
    """open_time is the M30 interval's opening UTC timestamp, in seconds."""

    open_time: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal

    def __post_init__(self) -> None:
        _time(self.open_time)
        if self.open_time % BAR_SECONDS:
            raise ValueError("Bar opening times must lie on UTC M30 boundaries.")
        for name in ("open", "high", "low", "close"):
            object.__setattr__(self, name, _price(getattr(self, name)))
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close):
            raise ValueError("High/low must contain the opening and closing prices.")

    @property
    def close_time(self) -> int:
        return self.open_time + BAR_SECONDS


@dataclass(frozen=True)
class OHLCBar(_Bar):
    """Raw observed OHLC prices, kept separate from synthetic HA prices."""


@dataclass(frozen=True)
class HeikenAshiBar(_Bar):
    """Synthetic HA values; these are not executable market quotes."""

    @property
    def color(self) -> Literal["red", "green", "doji"]:
        if self.close < self.open:
            return "red"
        if self.close > self.open:
            return "green"
        return "doji"


def _closed_sequence_bar(bar: _Bar, previous: int | None, as_of: int) -> None:
    _time(as_of)
    if bar.close_time > as_of:
        raise ValueError("Forming or future candles cannot produce signals.")
    if previous is not None:
        if bar.open_time <= previous:
            raise ValueError("Duplicate or out-of-order candle.")
        if bar.open_time != previous + BAR_SECONDS:
            raise ValueError("Missing M30 candle: replay a separate contiguous segment.")


def heiken_ashi(
    bars: Iterable[OHLCBar], *, timeframe: str, as_of: int,
) -> tuple[HeikenAshiBar, ...]:
    """Convert a fully closed, contiguous raw series using an explicit seed.

    First HA open = (first raw open + first raw close) / 2.
    Each HA close = (raw open + high + low + close) / 4.
    Later HA open = (previous HA open + previous HA close) / 2.
    HA high/low include raw high/low and the current HA open/close.
    Arithmetic uses a fixed 50-digit, half-even Decimal context.
    """
    _timeframe(timeframe)
    _time(as_of)
    raw = tuple(bars)
    previous = None
    for bar in raw:
        if type(bar) is not OHLCBar:
            raise ValueError("Convert raw OHLCBar inputs only; never convert HA twice.")
        _closed_sequence_bar(bar, previous, as_of)
        previous = bar.open_time
    result: list[HeikenAshiBar] = []
    with localcontext() as context:
        context.prec = 50
        context.rounding = ROUND_HALF_EVEN
        for bar in raw:
            opening = ((result[-1].open + result[-1].close) / 2 if result
                       else (bar.open + bar.close) / 2)
            closing = (bar.open + bar.high + bar.low + bar.close) / 4
            result.append(HeikenAshiBar(
                bar.open_time, opening, max(bar.high, opening, closing),
                min(bar.low, opening, closing), closing,
            ))
    return tuple(result)


@dataclass(frozen=True)
class SignalState:
    """Replay pattern state. long_signal_active is not a broker position."""

    symbol: str
    timeframe: str = TIMEFRAME
    last_open_time: int | None = None
    red_streak: int = 0
    long_signal_active: bool = False

    def __post_init__(self) -> None:
        _timeframe(self.timeframe)
        if (not isinstance(self.symbol, str) or not 1 <= len(self.symbol) <= 80
                or self.symbol != self.symbol.strip()
                or any(ord(char) < 32 or ord(char) == 127 for char in self.symbol)):
            raise ValueError("Supply the exact broker symbol as a nonempty identifier.")
        if self.last_open_time is not None:
            _time(self.last_open_time)
            if self.last_open_time % BAR_SECONDS:
                raise ValueError("State timestamp must lie on an M30 boundary.")
        if type(self.red_streak) is not int or self.red_streak < 0:
            raise ValueError("Invalid red streak.")
        if type(self.long_signal_active) is not bool:
            raise ValueError("Invalid replay signal state.")


@dataclass(frozen=True)
class Signal:
    symbol: str
    timeframe: str
    action: Literal["entry_long", "exit_long"]
    bar_open_time: int
    available_at: int
    preceding_red_bars: int

    @property
    def signal_id(self) -> str:
        return f"ha-m30:{self.symbol}:{self.bar_open_time}:{self.action}"


def advance(
    state: SignalState, bar: HeikenAshiBar, *, as_of: int,
) -> tuple[SignalState, Signal | None]:
    """Pure transition: >=3 reds then first green enters; next red exits.

    Dojis reset the red streak and never exit an active long signal.
    Duplicates, gaps and forming bars raise before any state transition.
    The exit red may begin the next red streak, but cannot also emit an entry.
    """
    if type(state) is not SignalState or type(bar) is not HeikenAshiBar:
        raise ValueError("Use SignalState and already converted HeikenAshiBar inputs.")
    _timeframe(state.timeframe)
    _closed_sequence_bar(bar, state.last_open_time, as_of)
    next_state = replace(state, last_open_time=bar.open_time)
    if state.long_signal_active:
        if bar.color == "red":
            return replace(next_state, long_signal_active=False, red_streak=1), Signal(
                state.symbol, TIMEFRAME, "exit_long", bar.open_time, bar.close_time, 0,
            )
        return replace(next_state, red_streak=0), None
    if bar.color == "red":
        return replace(next_state, red_streak=state.red_streak + 1), None
    if bar.color == "green" and state.red_streak >= 3:
        return replace(next_state, long_signal_active=True, red_streak=0), Signal(
            state.symbol, TIMEFRAME, "entry_long", bar.open_time, bar.close_time, state.red_streak,
        )
    return replace(next_state, red_streak=0), None


def replay_heiken_ashi(
    bars: Iterable[HeikenAshiBar], *, symbol: str, timeframe: str, as_of: int,
) -> tuple[Signal, ...]:
    """Replay supplied synthetic candles; no output is returned on invalid data."""
    state = SignalState(symbol=symbol, timeframe=timeframe)
    _time(as_of)
    signals = []
    for bar in bars:
        state, signal = advance(state, bar, as_of=as_of)
        if signal is not None:
            signals.append(signal)
    return tuple(signals)


def replay_ohlc(
    bars: Iterable[OHLCBar], *, symbol: str, timeframe: str, as_of: int,
) -> tuple[Signal, ...]:
    return replay_heiken_ashi(
        heiken_ashi(bars, timeframe=timeframe, as_of=as_of),
        symbol=symbol, timeframe=timeframe, as_of=as_of,
    )
