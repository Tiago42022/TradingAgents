"""Synthetic, standard-library-only tests. No broker imports or I/O."""

import unittest
from decimal import Decimal, localcontext

from .heiken_ashi_m30 import (
    BAR_SECONDS,
    HeikenAshiBar,
    OHLCBar,
    SignalState,
    advance,
    heiken_ashi,
    replay_heiken_ashi,
    replay_ohlc,
)


def ha(color, index, *, opening="100", body="1"):
    opening, body = Decimal(opening), Decimal(body)
    closing = opening + (body if color == "g" else -body if color == "r" else 0)
    return HeikenAshiBar(index * BAR_SECONDS, opening, max(opening, closing) + 1,
                        min(opening, closing) - 1, closing)


def replay(colors):
    return replay_heiken_ashi([ha(color, i) for i, color in enumerate(colors)],
                             symbol="GOLD.test", timeframe="M30", as_of=len(colors) * BAR_SECONDS)


class SignalTests(unittest.TestCase):
    def test_three_red_then_first_green_without_prior_trend(self):
        signals = replay("rrrg")
        self.assertEqual([s.action for s in signals], ["entry_long"])
        self.assertEqual(signals[0].preceding_red_bars, 3)
        self.assertEqual(signals[0].available_at, 7200)

    def test_at_least_three_includes_four_and_five(self):
        for count in (3, 4, 5, 12):
            with self.subTest(count=count):
                signals = replay("r" * count + "g")
                self.assertEqual(len(signals), 1)
                self.assertEqual(signals[0].preceding_red_bars, count)
        self.assertEqual(replay("rrg"), ())

    def test_first_green_consumes_streak_no_repeated_entry(self):
        self.assertEqual([s.action for s in replay("rrrgggg")], ["entry_long"])

    def test_no_body_size_or_prior_green_filter(self):
        bars = [ha("r", i, body="70") for i in range(3)] + [ha("g", 3, body="0.000001")]
        self.assertEqual(len(replay_heiken_ashi(bars, symbol="GOLD#", timeframe="M30", as_of=7200)), 1)

    def test_doji_breaks_red_streak(self):
        self.assertEqual(replay("rrrdg"), ())
        self.assertEqual(replay("rrdrrg"), ())
        self.assertEqual(len(replay("drrrg")), 1)

    def test_next_red_exits_but_doji_does_not(self):
        signals = replay("rrrgddggr")
        self.assertEqual([s.action for s in signals], ["entry_long", "exit_long"])
        self.assertEqual(signals[-1].bar_open_time, 8 * BAR_SECONDS)

    def test_exit_cannot_reenter_same_bar_and_can_start_next_streak(self):
        signals = replay("rrrgrrrg")
        self.assertEqual([s.action for s in signals], ["entry_long", "exit_long", "entry_long"])
        self.assertEqual(len({s.bar_open_time for s in signals}), len(signals))
        self.assertEqual(signals[-1].preceding_red_bars, 3)
        self.assertEqual(len({s.signal_id for s in signals}), len(signals))

    def test_forming_candle_boundary_and_pure_transition(self):
        state = SignalState("GOLD.test")
        with self.assertRaisesRegex(ValueError, "Forming"):
            advance(state, ha("r", 0), as_of=1799)
        self.assertEqual(state.red_streak, 0)
        updated, signal = advance(state, ha("r", 0), as_of=1800)
        self.assertIsNone(signal)
        self.assertEqual(updated.red_streak, 1)
        self.assertIsNone(state.last_open_time)

    def test_duplicate_and_out_of_order_never_emit_again(self):
        state = SignalState("GOLD.test")
        for i, color in enumerate("rrrg"):
            state, signal = advance(state, ha(color, i), as_of=7200)
        self.assertEqual(signal.action, "entry_long")
        for index in (3, 2):
            with self.assertRaisesRegex(ValueError, "Duplicate or out-of-order"):
                advance(state, ha("g", index), as_of=7200)
        self.assertTrue(state.long_signal_active)

    def test_gap_is_rejected_without_carrying_setup(self):
        state = SignalState("GOLD.test")
        for i in range(3):
            state, _ = advance(state, ha("r", i), as_of=9000)
        with self.assertRaisesRegex(ValueError, "Missing M30"):
            advance(state, ha("g", 4), as_of=9000)
        self.assertEqual(state.last_open_time, 3600)
        self.assertEqual(state.red_streak, 3)
        reset, signal = advance(SignalState("GOLD.test"), ha("g", 4), as_of=9000)
        self.assertIsNone(signal)
        self.assertEqual(reset.red_streak, 0)

    def test_m30_is_mandatory_on_both_entry_points(self):
        for timeframe in ("M1", "M15", "H1", "m30", "", 30):
            with self.subTest(timeframe=timeframe):
                with self.assertRaisesRegex(ValueError, "M30 only"):
                    replay_heiken_ashi([], symbol="GOLD", timeframe=timeframe, as_of=0)
                with self.assertRaisesRegex(ValueError, "M30 only"):
                    heiken_ashi([], timeframe=timeframe, as_of=0)

    def test_exact_symbol_is_carried_without_selection_or_aliasing(self):
        signals = replay_heiken_ashi([ha(c, i) for i, c in enumerate("rrrg")],
                                    symbol="GOLD.micro#", timeframe="M30", as_of=7200)
        self.assertEqual(signals[0].symbol, "GOLD.micro#")
        self.assertEqual(signals[0].timeframe, "M30")


class ConversionTests(unittest.TestCase):
    def test_standard_recurrence_and_explicit_first_seed(self):
        bars = [OHLCBar(0, "100", "110", "98", "108"),
                OHLCBar(1800, "108", "115", "105", "113"),
                OHLCBar(3600, "113", "114", "109", "110")]
        result = heiken_ashi(bars, timeframe="M30", as_of=5400)
        expected = [("104", "110", "98", "104"),
                    ("104", "115", "104", "110.25"),
                    ("107.125", "114", "107.125", "111.5")]
        for bar, values in zip(result, expected, strict=True):
            self.assertEqual((bar.open, bar.high, bar.low, bar.close), tuple(map(Decimal, values)))
        self.assertEqual(result[0].color, "doji")

    def test_conversion_context_does_not_depend_on_callers_precision(self):
        bars = [OHLCBar(0, "100.123456", "110", "98", "108.123456")]
        normal = heiken_ashi(bars, timeframe="M30", as_of=1800)
        with localcontext() as context:
            context.prec = 4
            self.assertEqual(heiken_ashi(bars, timeframe="M30", as_of=1800), normal)

    def test_raw_conversion_and_replay_entry_exit(self):
        bars = [OHLCBar(0, "10", "10", "6", "10"),
                OHLCBar(1800, "8", "8", "4", "8"),
                OHLCBar(3600, "6", "6", "2", "6"),
                OHLCBar(5400, "10", "12", "10", "10"),
                OHLCBar(7200, "4", "5", "3", "4")]
        self.assertEqual([bar.color for bar in heiken_ashi(bars, timeframe="M30", as_of=9000)],
                         ["red", "red", "red", "green", "red"])
        self.assertEqual([s.action for s in replay_ohlc(bars, symbol="GOLD.test", timeframe="M30", as_of=9000)],
                         ["entry_long", "exit_long"])

    def test_conversion_rejects_forming_duplicate_reversed_and_gapped_data(self):
        first, second = OHLCBar(0, 10, 11, 9, 10), OHLCBar(1800, 10, 11, 9, 10)
        for bars, as_of in (([first, second], 3599), ([first, first], 3600),
                            ([second, first], 3600), ([first, OHLCBar(3600, 10, 11, 9, 10)], 5400)):
            with self.subTest(bars=bars, as_of=as_of):
                with self.assertRaises(ValueError):
                    heiken_ashi(bars, timeframe="M30", as_of=as_of)

    def test_input_types_and_prices_are_validated(self):
        for timestamp in (True, -1800, 1, 1800.0):
            with self.assertRaises(ValueError):
                OHLCBar(timestamp, 10, 11, 9, 10)
        for price in ("NaN", "Infinity", "0", "-1", True, 10.0):
            with self.assertRaises(ValueError):
                OHLCBar(0, price, 11, 9, 10)
        with self.assertRaises(ValueError):
            OHLCBar(0, 10, 9, 8, 10)
        with self.assertRaises(ValueError):
            OHLCBar(0, 10, 12, 11, 10)
        with self.assertRaises(ValueError):
            heiken_ashi([ha("r", 0)], timeframe="M30", as_of=1800)
        with self.assertRaises(ValueError):
            advance(SignalState("GOLD.test"), OHLCBar(0, 10, 11, 9, 10), as_of=1800)


if __name__ == "__main__":
    unittest.main()
