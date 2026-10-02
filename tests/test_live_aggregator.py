from datetime import datetime, timedelta
import unittest
from zoneinfo import ZoneInfo

from tw_quant.market import TickEvent, TradingCalendar, classify_tmf_session
from tw_quant.live.aggregator import MinuteBarAggregator


TZ = ZoneInfo("Asia/Taipei")


def tick(value: str, price: float, volume: int, sequence: str) -> TickEvent:
    exchange_time = datetime.fromisoformat(value).replace(tzinfo=TZ)
    return TickEvent(
        symbol="TMF", contract="TMFI6", exchange_time=exchange_time,
        received_time=exchange_time + timedelta(milliseconds=10),
        price=price, volume=volume, sequence=sequence,
    )


class LiveAggregatorTests(unittest.TestCase):
    def test_same_minute_updates_ohlcv(self):
        aggregator = MinuteBarAggregator()
        aggregator.process(tick("2026-08-24T15:00:20", 100, 2, "1"))
        aggregator.process(tick("2026-08-24T15:00:40", 103, 3, "2"))
        result = aggregator.process(tick("2026-08-24T15:00:10", 99, 1, "3"))
        bar = result.bars[-1]
        self.assertEqual((bar.open, bar.high, bar.low, bar.close, bar.volume), (99, 103, 99, 103, 6))
        self.assertEqual(bar.status, "forming")

    def test_cross_minute_closes_old_and_creates_new(self):
        aggregator = MinuteBarAggregator()
        aggregator.process(tick("2026-08-24T15:00:58", 100, 1, "1"))
        result = aggregator.process(tick("2026-08-24T15:01:01", 101, 2, "2"))
        self.assertEqual([bar.status for bar in result.bars], ["closed", "forming"])
        self.assertEqual(result.bars[1].open, 101)

    def test_no_trade_minute_is_filled_with_zero_volume(self):
        aggregator = MinuteBarAggregator()
        aggregator.process(tick("2026-08-24T15:00:58", 100, 1, "1"))
        result = aggregator.process(tick("2026-08-24T15:02:01", 102, 1, "2"))
        gap = result.bars[1]
        self.assertTrue(gap.no_trade)
        self.assertEqual(gap.volume, 0)
        self.assertEqual((gap.open, gap.high, gap.low, gap.close), (100, 100, 100, 100))

    def test_duplicate_tick_is_ignored(self):
        aggregator = MinuteBarAggregator()
        item = tick("2026-08-24T15:00:01", 100, 2, "same")
        aggregator.process(item)
        result = aggregator.process(item)
        self.assertTrue(result.duplicate)
        self.assertEqual(aggregator.current.volume, 2)

    def test_late_tick_for_closed_bar_is_ignored(self):
        aggregator = MinuteBarAggregator()
        aggregator.process(tick("2026-08-24T15:00:50", 100, 1, "1"))
        aggregator.process(tick("2026-08-24T15:01:01", 101, 1, "2"))
        result = aggregator.process(tick("2026-08-24T15:00:59", 90, 1, "3"))
        self.assertTrue(result.late)
        self.assertEqual(aggregator.current.open, 101)

    def test_contract_rollover_closes_old_contract(self):
        aggregator = MinuteBarAggregator()
        aggregator.process(tick("2026-08-24T15:00:50", 100, 1, "1"))
        next_tick = tick("2026-08-24T15:00:55", 101, 1, "2")
        next_tick = TickEvent(
            symbol=next_tick.symbol, contract="TMFJ6",
            exchange_time=next_tick.exchange_time,
            received_time=next_tick.received_time, price=next_tick.price,
            volume=next_tick.volume, sequence=next_tick.sequence,
        )
        result = aggregator.process(next_tick)
        self.assertEqual(result.bars[0].status, "closed")
        self.assertEqual(result.bars[0].contract, "TMFI6")
        self.assertEqual(result.bars[1].contract, "TMFJ6")

    def test_night_session_crosses_calendar_date(self):
        before_midnight = datetime(2026, 8, 24, 23, 59, tzinfo=TZ)
        after_midnight = datetime(2026, 8, 25, 0, 1, tzinfo=TZ)
        self.assertEqual(classify_tmf_session(before_midnight), ("night", datetime(2026, 8, 25).date()))
        self.assertEqual(classify_tmf_session(after_midnight), ("night", datetime(2026, 8, 25).date()))

    def test_night_trading_date_skips_configured_holiday(self):
        holiday = datetime(2026, 8, 25).date()
        calendar = TradingCalendar(frozenset({holiday}))
        session, trading_date = classify_tmf_session(
            datetime(2026, 8, 24, 15, 1, tzinfo=TZ), calendar
        )
        self.assertEqual(session, "night")
        self.assertEqual(trading_date, datetime(2026, 8, 26).date())


if __name__ == "__main__":
    unittest.main()
