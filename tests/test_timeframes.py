from datetime import date, datetime, timedelta
import unittest
from zoneinfo import ZoneInfo

from tw_quant.market import (
    KBar,
    TimeframeStreamAggregator,
    aggregate_kbars,
    timeframe_bucket,
    validate_timeframe,
)


TAIPEI = ZoneInfo("Asia/Taipei")


def make_bar(
    timestamp: datetime,
    close: float,
    *,
    open_price: float | None = None,
    volume: int = 1,
    status: str = "closed",
    session: str = "night",
    trading_date: date = date(2026, 8, 25),
    contract: str = "TMFU6",
) -> KBar:
    return KBar(
        symbol="TMF", contract=contract, time=timestamp,
        open=close if open_price is None else open_price,
        high=close + 2, low=close - 2, close=close, volume=volume,
        status=status, session=session, trading_date=trading_date,
        first_tick_time=timestamp, last_tick_time=timestamp + timedelta(seconds=50),
        exchange_time=timestamp + timedelta(seconds=50),
        received_time=timestamp + timedelta(seconds=50, milliseconds=12),
        latency_ms=12,
    )


class TimeframeTests(unittest.TestCase):
    def test_supported_values_and_invalid_interval(self):
        for value in ("1m", "5m", "10m", "15m", "30m", "1h", "1d", "1w"):
            self.assertEqual(validate_timeframe(value), value)
        with self.assertRaisesRegex(ValueError, "unsupported interval"):
            validate_timeframe("2h")

    def test_five_minute_ohlcv_uses_night_session_anchor(self):
        base = datetime(2026, 8, 24, 15, 0, tzinfo=TAIPEI)
        bars = [
            make_bar(base + timedelta(minutes=index), 100 + index, volume=index + 1)
            for index in range(6)
        ]
        result = aggregate_kbars(bars, "5m")
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0].time, base)
        self.assertEqual(
            (result[0].open, result[0].high, result[0].low, result[0].close),
            (100, 106, 98, 104),
        )
        self.assertEqual(result[0].volume, 15)
        self.assertEqual(result[1].time, base + timedelta(minutes=5))

    def test_day_session_buckets_start_at_0845(self):
        timestamp = datetime(2026, 8, 25, 9, 2, tzinfo=TAIPEI)
        bar = make_bar(
            timestamp, 100, session="day", trading_date=date(2026, 8, 25)
        )
        self.assertEqual(
            timeframe_bucket(bar, "15m")[-1],
            datetime(2026, 8, 25, 9, 0, tzinfo=TAIPEI),
        )
        self.assertEqual(
            timeframe_bucket(bar, "1h")[-1],
            datetime(2026, 8, 25, 8, 45, tzinfo=TAIPEI),
        )

    def test_daily_combines_cross_midnight_night_and_day_by_trading_date(self):
        bars = [
            make_bar(datetime(2026, 8, 24, 15, 0, tzinfo=TAIPEI), 100),
            make_bar(datetime(2026, 8, 25, 1, 0, tzinfo=TAIPEI), 95),
            make_bar(
                datetime(2026, 8, 25, 8, 45, tzinfo=TAIPEI), 110,
                session="day",
            ),
        ]
        result = aggregate_kbars(bars, "1d")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].time, bars[0].time)
        self.assertEqual(result[0].trading_date, date(2026, 8, 25))
        self.assertEqual(result[0].close, 110)
        self.assertEqual(result[0].low, 93)

    def test_contract_roll_never_merges_bars(self):
        timestamp = datetime(2026, 8, 24, 15, 0, tzinfo=TAIPEI)
        bars = [make_bar(timestamp, 100), make_bar(timestamp, 200, contract="TMFV6")]
        result = aggregate_kbars(bars, "1d")
        self.assertEqual([bar.contract for bar in result], ["TMFU6", "TMFV6"])

    def test_stream_replaces_forming_minute_and_closes_previous_bucket(self):
        base = datetime(2026, 8, 24, 15, 0, tzinfo=TAIPEI)
        first = make_bar(base, 100, volume=2, status="forming")
        stream = TimeframeStreamAggregator("5m", [first])
        update = make_bar(base, 103, open_price=100, volume=5, status="forming")
        current = stream.push(update)
        self.assertEqual(len(current), 1)
        self.assertEqual((current[0].open, current[0].close, current[0].volume), (100, 103, 5))
        next_bucket = make_bar(base + timedelta(minutes=5), 104, status="forming")
        emitted = stream.push(next_bucket)
        self.assertEqual([bar.status for bar in emitted], ["closed", "forming"])
        self.assertEqual(emitted[1].time, base + timedelta(minutes=5))


if __name__ == "__main__":
    unittest.main()
