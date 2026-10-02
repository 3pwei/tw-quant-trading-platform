from datetime import date
import unittest

import pandas as pd

from tw_quant.execution import SignalSimulationPolicy, simulate_signals


def bars() -> pd.DataFrame:
    timestamps = pd.date_range(
        "2026-09-09 08:45",
        periods=5,
        freq="min",
        tz="Asia/Taipei",
    )
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": [100.0] * 5,
            "high": [100.1] * 5,
            "low": [99.9] * 5,
            "close": [100.0] * 5,
            "contract": ["TMFU6"] * 5,
            "session": ["day"] * 5,
            "trading_date": [date(2026, 9, 9).isoformat()] * 5,
        }
    )


class SignalSimulatorPolicyTests(unittest.TestCase):
    def test_entry_keeps_trigger_bar_time_and_context_until_next_open_fill(self):
        frame = bars()
        entries = pd.Series([1, 0, 0, 0, 0], index=frame.index)
        context = pd.DataFrame(
            {"rsi": [28.4, 72.0, 60.0, 50.0, 40.0]}, index=frame.index
        )

        signals = simulate_signals(
            frame, "test", entries, diagnostic_context=context
        )

        entry = signals[0]
        self.assertEqual(
            entry["trigger_time"],
            frame.iloc[0]["timestamp"].isoformat(timespec="milliseconds"),
        )
        self.assertEqual(
            entry["time"],
            frame.iloc[1]["timestamp"].isoformat(timespec="milliseconds"),
        )
        self.assertEqual(entry["context"], {"rsi": 28.4})

    def test_default_policy_allows_distinct_reentries_in_one_group(self):
        frame = pd.concat([bars(), bars().iloc[:2]], ignore_index=True)
        frame["timestamp"] = pd.date_range(
            "2026-09-09 08:45", periods=7, freq="min", tz="Asia/Taipei"
        )
        entries = pd.Series([1, 0, 1, 0, 1, 0, 0], index=frame.index)
        exits = pd.DataFrame(
            {"long": [False, True, False, True, False, True, False],
             "short": [False] * 7},
            index=frame.index,
        )

        signals = simulate_signals(frame, "test", entries, exits)

        self.assertEqual(
            [signal["event"] for signal in signals],
            ["entry", "exit", "entry", "exit", "entry", "exit"],
        )

    def test_sustained_intent_must_reset_before_same_direction_reentry(self):
        frame = pd.concat([bars(), bars().iloc[:1]], ignore_index=True)
        frame["timestamp"] = pd.date_range(
            "2026-09-09 08:45", periods=6, freq="min", tz="Asia/Taipei"
        )
        entries = pd.Series([1, 1, 1, 0, 1, 0], index=frame.index)
        exits = pd.DataFrame(
            {"long": [False, True, False, False, False, False],
             "short": [False] * 6},
            index=frame.index,
        )

        signals = simulate_signals(
            frame,
            "test",
            entries,
            exits,
            policy=SignalSimulationPolicy(
                strategy_exit_reason="channel_invalidation"
            ),
        )

        self.assertEqual(
            [signal["event"] for signal in signals],
            ["entry", "exit", "entry"],
        )
        self.assertEqual(signals[1]["reason"], "channel_invalidation")

    def test_policy_rejects_invalid_configuration(self):
        with self.assertRaisesRegex(ValueError, "strategy_exit_reason"):
            SignalSimulationPolicy(strategy_exit_reason=" ")


if __name__ == "__main__":
    unittest.main()
