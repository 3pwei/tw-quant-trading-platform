from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import timedelta

from ..market import (
    DEFAULT_CALENDAR,
    KBar,
    TickEvent,
    TradingCalendar,
    classify_tmf_session,
    minute_floor,
)


@dataclass
class AggregateResult:
    bars: list[KBar] = field(default_factory=list)
    duplicate: bool = False
    late: bool = False


class MinuteBarAggregator:
    """Aggregate validated exchange-time ticks into deterministic one-minute bars."""

    def __init__(
        self,
        symbol: str = "TMF",
        dedup_capacity: int = 100_000,
        calendar: TradingCalendar = DEFAULT_CALENDAR,
    ):
        self.symbol = symbol
        self.dedup_capacity = dedup_capacity
        self.calendar = calendar
        self.current: KBar | None = None
        self._seen: OrderedDict[str, None] = OrderedDict()
        self.duplicate_ticks = 0
        self.late_ticks = 0

    def restore(self, bar: KBar | None) -> None:
        self.current = bar if bar and bar.status == "forming" else None

    def _remember(self, key: str) -> bool:
        if key in self._seen:
            self.duplicate_ticks += 1
            return False
        self._seen[key] = None
        if len(self._seen) > self.dedup_capacity:
            self._seen.popitem(last=False)
        return True

    def _new_bar(self, tick: TickEvent) -> KBar:
        session, trading_date = classify_tmf_session(tick.exchange_time, self.calendar)
        latency = max(0.0, (tick.received_time - tick.exchange_time).total_seconds() * 1000)
        return KBar(
            symbol=tick.symbol,
            contract=tick.contract,
            time=minute_floor(tick.exchange_time),
            open=tick.price,
            high=tick.price,
            low=tick.price,
            close=tick.price,
            volume=tick.volume,
            status="forming",
            session=session,
            trading_date=trading_date,
            first_tick_time=tick.exchange_time,
            last_tick_time=tick.exchange_time,
            exchange_time=tick.exchange_time,
            received_time=tick.received_time,
            latency_ms=latency,
        )

    def _gap_bars(self, start: KBar, target_minute) -> list[KBar]:
        bars: list[KBar] = []
        cursor = start.time + timedelta(minutes=1)
        while cursor < target_minute:
            try:
                session, trading_date = classify_tmf_session(cursor, self.calendar)
            except ValueError:
                cursor += timedelta(minutes=1)
                continue
            if session != start.session or trading_date != start.trading_date:
                break
            bars.append(
                start.copy(
                    time=cursor,
                    open=start.close,
                    high=start.close,
                    low=start.close,
                    close=start.close,
                    volume=0,
                    status="closed",
                    first_tick_time=cursor,
                    last_tick_time=cursor,
                    exchange_time=cursor,
                    received_time=start.received_time,
                    latency_ms=0.0,
                    no_trade=True,
                )
            )
            cursor += timedelta(minutes=1)
        return bars

    def process(self, tick: TickEvent) -> AggregateResult:
        if tick.symbol != self.symbol:
            raise ValueError(f"unexpected symbol: {tick.symbol}")
        if not self._remember(tick.dedup_key):
            return AggregateResult(duplicate=True)

        minute = minute_floor(tick.exchange_time)
        if self.current is None:
            self.current = self._new_bar(tick)
            return AggregateResult([self.current.copy()])

        if minute < self.current.time:
            self.late_ticks += 1
            return AggregateResult(late=True)

        if tick.contract != self.current.contract or minute > self.current.time:
            closed = self.current.copy(status="closed")
            bars = [closed]
            if tick.contract == self.current.contract:
                bars.extend(self._gap_bars(closed, minute))
            self.current = self._new_bar(tick)
            bars.append(self.current.copy())
            return AggregateResult(bars)

        latency = max(0.0, (tick.received_time - tick.exchange_time).total_seconds() * 1000)
        if tick.exchange_time < self.current.first_tick_time:
            self.current.open = tick.price
            self.current.first_tick_time = tick.exchange_time
        if tick.exchange_time >= self.current.last_tick_time:
            self.current.close = tick.price
            self.current.last_tick_time = tick.exchange_time
        self.current.high = max(self.current.high, tick.price)
        self.current.low = min(self.current.low, tick.price)
        self.current.volume += tick.volume
        self.current.exchange_time = max(self.current.exchange_time, tick.exchange_time)
        self.current.received_time = tick.received_time
        self.current.latency_ms = latency
        self.current.no_trade = False
        return AggregateResult([self.current.copy()])

    def close_current(self) -> KBar | None:
        if self.current is None:
            return None
        closed = self.current.copy(status="closed")
        self.current = None
        return closed
