
from public_fixtures import InertProvider, PublicTestCase, PublicAsyncTestCase, synthetic_csv, services
from datetime import date, datetime, timedelta
import tempfile
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

from tw_quant.backtest import (
    run_historical_events,
    run_strategy_backtest,
    validate_date_range,
)
from tw_quant.market import KBar
from tw_quant.live.storage import SQLiteBarRepository


TAIPEI = ZoneInfo("Asia/Taipei")


def make_bar(minute: int, close: float, volume: int = 100) -> KBar:
    timestamp = datetime(2026, 8, 24, 15, 0, tzinfo=TAIPEI) + timedelta(minutes=minute)
    return KBar(
        symbol="TMF", contract="TMFU6", time=timestamp, open=close,
        high=close + 0.2, low=close - 0.2, close=close, volume=volume,
        status="closed", session="night", trading_date=date(2026, 8, 25),
        first_tick_time=timestamp, last_tick_time=timestamp + timedelta(seconds=50),
        exchange_time=timestamp + timedelta(seconds=50),
        received_time=timestamp + timedelta(seconds=50, milliseconds=10),
        latency_ms=10,
    )


class StrategyBacktestTests(PublicTestCase):
    def test_historical_runner_emits_auditable_execution_chain_deterministically(self):
        bars = [make_bar(0, 100.0), make_bar(1, 105.0)]
        signals = [
            {
                "strategy": "test",
                "event": "entry",
                "direction": "long",
                "time": bars[0].time.isoformat(timespec="milliseconds"),
                "price": 100.0,
                "stop_loss_price": 98.0,
                "take_profit_price": 106.0,
                "reason": "signal_confirmed",
                "trigger_reason": "test_indicator_crossed",
                "context": {"test_indicator": 1.25},
                "contract": bars[0].contract,
                "trading_date": bars[0].trading_date.isoformat(),
            },
            {
                "strategy": "test",
                "event": "exit",
                "direction": "long",
                "time": bars[1].time.isoformat(timespec="milliseconds"),
                "price": 105.0,
                "stop_loss_price": 98.0,
                "take_profit_price": 106.0,
                "reason": "test_exit",
                "context": {"test_indicator": 0.25},
                "contract": bars[1].contract,
                "trading_date": bars[1].trading_date.isoformat(),
            },
        ]
        first = run_historical_events(bars, signals, strategy_id="test")
        second = run_historical_events(bars, signals, strategy_id="test")

        self.assertEqual(first.execution_events, second.execution_events)
        self.assertEqual(first.event_counts["signal"], 2)
        self.assertEqual(first.event_counts["order_intent"], 2)
        self.assertEqual(first.event_counts["risk_decision"], 2)
        self.assertEqual(first.event_counts["fill"], 2)
        self.assertEqual(len(first.trades), 1)
        self.assertEqual(first.trades[0]["exit_reason"], "test_exit")
        self.assertEqual(first.trades[0]["entry_reason"], "test_indicator_crossed")
        self.assertEqual(first.trades[0]["entry_context"], {"test_indicator": 1.25})
        self.assertEqual(first.trades[0]["exit_context"], {"test_indicator": 0.25})
        for event in first.execution_events:
            if event["kind"] in {"order_intent", "risk_decision", "fill"}:
                self.assertIsNotNone(event["meta"]["causation_id"])

    def test_range_is_inclusive_and_limited_to_31_days(self):
        validate_date_range(date(2026, 8, 1), date(2026, 8, 31))
        with self.assertRaisesRegex(ValueError, "31 天"):
            validate_date_range(date(2026, 8, 1), date(2026, 9, 1))
        with self.assertRaisesRegex(ValueError, "開始日期"):
            validate_date_range(date(2026, 8, 2), date(2026, 8, 1))





    def test_backtest_reports_selected_timeframe(self):
        bars = [make_bar(index, 100.0) for index in range(20)]
        result = run_strategy_backtest(
            bars, "scripted", date(2026, 8, 25), date(2026, 8, 25), interval="5m"
        )
        self.assertEqual(result["metadata"]["interval"], "5 分鐘")
        self.assertEqual(result["metadata"]["interval_key"], "5m")
        self.assertEqual(result["metadata"]["interval_key"], "5m")

    def test_repository_filters_closed_bars_by_trading_date(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = SQLiteBarRepository(Path(directory) / "bars.sqlite3")
            try:
                first = make_bar(0, 100)
                forming = make_bar(1, 101)
                forming.status = "forming"
                repo.save(first)
                repo.save(forming)
                self.assertEqual(
                    repo.date_bounds("TMF"),
                    (date(2026, 8, 25), date(2026, 8, 25)),
                )
                selected = repo.between_trading_dates(
                    "TMF", date(2026, 8, 25), date(2026, 8, 25)
                )
                self.assertEqual([bar.time for bar in selected], [first.time])
            finally:
                repo.close()

    def test_strategy_parameters_persist_across_repository_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bars.sqlite3"
            first = SQLiteBarRepository(path)
            first.save_strategy_parameters(
                "scripted",
                {
                    "window": 10,
                    "volume_window": 8,
                    "scale": 1.5,
                    "stop_loss_pct": 0.01,
                    "take_profit_pct": 0.025,
                },
            )
            first.close()
            reopened = SQLiteBarRepository(path)
            try:
                self.assertEqual(
                    reopened.strategy_parameters()["scripted"]["window"],
                    10,
                )
            finally:
                reopened.close()


if __name__ == "__main__":
    unittest.main()
