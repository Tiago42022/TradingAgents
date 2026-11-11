# Archive M30 Heiken Ashi signal prototype

Research and replay only. This package uses Python's standard library and has no connection to the manual MT5 executor, account credentials, order APIs or position-closing functions. It produces signal observations, not orders or simulated profits.

The current specification targets the GOLD market. Supply an exact symbol label explicitly, such as the identifier in an exported dataset; the package neither discovers an XM instrument nor selects a live account.

The rules are deliberately limited:

- Use **M30 only**, with fully closed Heiken Ashi candles.
- At least three consecutive red candles followed immediately by the first green candle produces `entry_long`.
- After that entry signal, the next red candle produces `exit_long`.
- A doji is exact HA close/open equality. It breaks a red streak and does not produce an exit.
- There is no preceding-green/uptrend, body-size, EMA, M1 timing, sizing, risk ladder, stop-loss or take-profit filter.
- A single bar emits at most one signal. An exit red can be the first red of a new setup; a later green still requires three consecutive reds.

`SignalState.long_signal_active` records the pattern's replay state, not whether an account actually holds a position. No live strategy is activated by importing or running these functions.

## Data and time contract

`open_time` and `as_of` are integer UTC epoch **seconds**, not milliseconds. Bar opening times must lie on a 30-minute boundary, increase strictly and remain exactly 1,800 seconds apart. A candle is closed when `open_time + 1800 <= as_of`. Forming/future bars, duplicates, out-of-order bars and gaps raise `ValueError`. A rejected transition leaves its immutable input state unchanged. Start an explicitly separate replay segment after a data or market-session gap; no streak is inferred across missing bars.

Prices are positive finite `Decimal`, decimal strings or integers; binary floats are rejected. Raw and HA candles use separate types to prevent applying the conversion twice. Direct HA fixtures can be passed to `replay_heiken_ashi`; raw data goes through `replay_ohlc`.

The conversion follows the [MetaQuotes Heiken Ashi recurrence](https://www.mql5.com/en/code/33): HA close averages the current raw OHLC values, later HA open averages the preceding HA open and close, and HA high/low encompass the raw extreme and current synthetic body. This implementation explicitly seeds the first HA open with the average of the first raw open and close. Calculations use a fixed 50-digit, half-even decimal context. Different history start points or platform seed conventions can change early candles; compare against the intended chart over a shared history before drawing conclusions.

**HA prices are synthetic and are not executable market quotes.** Signals become available at candle close; this package makes no assumption about execution price, slippage, spread or fill time. It does not establish profitability.

## Example and offline tests

```python
from integrations.archive_strategies import OHLCBar, replay_ohlc

# Synthetic research data, not a live instrument selection or market feed.
bars = [
    OHLCBar(0, "10", "10", "6", "10"),
    OHLCBar(1800, "8", "8", "4", "8"),
    OHLCBar(3600, "6", "6", "2", "6"),
    OHLCBar(5400, "10", "12", "10", "10"),
]
signals = replay_ohlc(bars, symbol="GOLD.example", timeframe="M30", as_of=7200)
assert signals[0].action == "entry_long"
```

Run from the repository root without installing broker or LLM dependencies:

```console
python -m unittest integrations.archive_strategies.test_heiken_ashi_m30 -v
```

The workspace search found an existing Archive demo with bullish HA + EMA/ATR rules, which differs from this specification. The PDF's separate paper-engine claim was not verified; no rules, risk amounts or performance claims from that document are activated here.
