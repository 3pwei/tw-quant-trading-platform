from __future__ import annotations

import asyncio
from datetime import datetime
from time import perf_counter
from typing import Callable

from ..market import (
    DEFAULT_CALENDAR,
    TAIPEI,
    ConnectionStatus,
    ExecutionQuote,
    ExecutionQuoteCache,
    ExecutionQuoteSink,
    KBar,
    TickEvent,
    TradingCalendar,
    isoformat_millis,
)
from .aggregator import MinuteBarAggregator
from ..market_data import HistoricalMarketDataProvider, LiveMarketDataProvider
from .hub import BroadcastHub
from .storage import MarketRepository


class LiveMarketService:
    def __init__(
        self,
        feed: LiveMarketDataProvider,
        repository: MarketRepository,
        symbol: str = "TMF",
        heartbeat_seconds: float = 5.0,
        calendar: TradingCalendar = DEFAULT_CALENDAR,
        history_limit: int = 500,
        history_provider: HistoricalMarketDataProvider | None = None,
        stale_after_seconds: float = 120.0,
        execution_quote_sink: ExecutionQuoteSink | None = None,
    ):
        # ``feed`` remains the constructor name for compatibility. Internally
        # the service depends on provider-neutral ports.
        self.market_data_provider = feed
        self.history_provider = history_provider
        self.repository = repository
        self.symbol = symbol
        self.heartbeat_seconds = heartbeat_seconds
        self.history_limit = history_limit
        self.stale_after_seconds = stale_after_seconds
        self.queue: asyncio.Queue[TickEvent] = asyncio.Queue(maxsize=20_000)
        self.execution_quotes = ExecutionQuoteCache()
        self.execution_quote_sink = execution_quote_sink
        self.execution_quote_queue: asyncio.Queue[ExecutionQuote] = asyncio.Queue(
            maxsize=2_048
        )
        self.hub = BroadcastHub()
        self.aggregator = MinuteBarAggregator(symbol, calendar=calendar)
        self.connection_status: ConnectionStatus = "connecting"
        self.last_tick_time: datetime | None = None
        self.last_received_time: datetime | None = None
        self.last_bar_time: datetime | None = None
        self.last_bar_received_time: datetime | None = None
        self.last_heartbeat_time: datetime | None = None
        self.dropped_ticks = 0
        self.processed_ticks = 0
        self.worker_cycles = 0
        self.worker_errors = 0
        self.last_worker_error: str | None = None
        self.queue_high_watermark = 0
        self.total_tick_processing_ms = 0.0
        self.max_tick_processing_ms = 0.0
        self.database_write_count = 0
        self.total_database_write_ms = 0.0
        self.max_database_write_ms = 0.0
        self.last_database_write_ms: float | None = None
        self.history_bars_loaded = 0
        self.history_error: str | None = None
        self._worker: asyncio.Task | None = None
        self._heartbeat: asyncio.Task | None = None
        self._quote_worker: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._running = False
        self._bar_listeners: list[Callable[[KBar], None]] = []
        quote_registration = getattr(
            self.market_data_provider, "set_execution_quote_callback", None
        )
        if callable(quote_registration):
            quote_registration(self._receive_execution_quote)

    def _receive_execution_quote(self, quote: ExecutionQuote) -> None:
        """Callback hot path: update memory and enqueue; never write SQLite."""

        self.execution_quotes.update(quote)
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(self._enqueue_execution_quote, quote)

    def _enqueue_execution_quote(self, quote: ExecutionQuote) -> None:
        try:
            self.execution_quote_queue.put_nowait(quote)
        except asyncio.QueueFull:
            # The cache still has the newest quote. Protection fails closed on
            # stale persistence instead of blocking the provider callback.
            pass

    def add_bar_listener(self, listener: Callable[[KBar], None]) -> None:
        if listener not in self._bar_listeners:
            self._bar_listeners.append(listener)

    def remove_bar_listener(self, listener: Callable[[KBar], None]) -> None:
        if listener in self._bar_listeners:
            self._bar_listeners.remove(listener)

    def enqueue_tick(self, tick: TickEvent) -> None:
        try:
            self.queue.put_nowait(tick)
            self.queue_high_watermark = max(
                self.queue_high_watermark, self.queue.qsize()
            )
        except asyncio.QueueFull:
            self.dropped_ticks += 1

    def set_connection_status(self, status: ConnectionStatus) -> None:
        self.connection_status = status
        self.hub.publish(self.status_message())

    async def start(self) -> None:
        self._running = True
        self._loop = asyncio.get_running_loop()
        restored = self.repository.latest_forming(self.symbol)
        self.aggregator.restore(restored)
        latest = self.repository.latest(self.symbol, 1)
        if latest:
            self._remember_bar(latest[0])
        self._worker = asyncio.create_task(self._run_worker(), name="tmf-kbar-worker")
        self._heartbeat = asyncio.create_task(
            self._run_heartbeat(), name="tmf-feed-heartbeat"
        )
        if self.execution_quote_sink is not None:
            self._quote_worker = asyncio.create_task(
                self._run_execution_quote_writer(), name="execution-quote-writer"
            )
        await self.market_data_provider.start(
            self.enqueue_tick, self.set_connection_status
        )
        try:
            history = (
                await self.history_provider.load_history(self.history_limit)
                if self.history_provider is not None
                else []
            )
            if history:
                purge = getattr(self.repository, "purge_backfill", None)
                if purge is not None:
                    purge(self.symbol, history[-1].contract)
            for bar in history:
                self._save_bar(bar)
            self.history_bars_loaded = len(history)
        except Exception as exc:
            # Historical backfill must not interrupt the live Tick stream.
            self.history_error = str(exc)

    async def stop(self) -> None:
        self._running = False
        await self.market_data_provider.stop()
        for task in (self._worker, self._heartbeat, self._quote_worker):
            if task:
                task.cancel()
        await asyncio.gather(
            *(task for task in (self._worker, self._heartbeat, self._quote_worker) if task),
            return_exceptions=True,
        )
        self._loop = None
        current = self.aggregator.current
        if current:
            self._save_bar(current)

    def _record_database_write(self, started: float) -> None:
        elapsed_ms = (perf_counter() - started) * 1_000
        self.database_write_count += 1
        self.total_database_write_ms += elapsed_ms
        self.max_database_write_ms = max(self.max_database_write_ms, elapsed_ms)
        self.last_database_write_ms = elapsed_ms

    def _remember_tick(self, tick: TickEvent) -> None:
        started = perf_counter()
        self.repository.remember_tick(tick.dedup_key, tick.exchange_time)
        self._record_database_write(started)

    def _remember_bar(self, bar: KBar) -> None:
        if (
            self.last_bar_received_time is None
            or bar.received_time >= self.last_bar_received_time
        ):
            self.last_bar_time = bar.time
            self.last_bar_received_time = bar.received_time

    def _save_bar(self, bar: KBar) -> None:
        started = perf_counter()
        self.repository.save(bar)
        self._record_database_write(started)
        self._remember_bar(bar)

    async def _run_worker(self) -> None:
        while self._running:
            tick = await self.queue.get()
            started = perf_counter()
            try:
                self.last_tick_time = tick.exchange_time
                self.last_received_time = tick.received_time
                if self.repository.tick_seen(tick.dedup_key):
                    self.aggregator.duplicate_ticks += 1
                    continue
                self._remember_tick(tick)
                result = self.aggregator.process(tick)
                for bar in result.bars:
                    self._save_bar(bar)
                    self.hub.publish(bar.to_message(self.connection_status))
                    for listener in tuple(self._bar_listeners):
                        listener(bar)
                self.processed_ticks += 1
            except Exception as exc:
                # One malformed Tick or downstream listener must not terminate
                # the only market-data worker. The diagnostic remains admin-only.
                self.worker_errors += 1
                self.last_worker_error = f"{type(exc).__name__}: {exc}"
            finally:
                elapsed_ms = (perf_counter() - started) * 1_000
                self.worker_cycles += 1
                self.total_tick_processing_ms += elapsed_ms
                self.max_tick_processing_ms = max(
                    self.max_tick_processing_ms, elapsed_ms
                )
                self.queue.task_done()

    async def _run_execution_quote_writer(self) -> None:
        assert self.execution_quote_sink is not None
        while self._running:
            quote = await self.execution_quote_queue.get()
            latest = {(quote.symbol, quote.contract): quote}
            # Bound cross-process SQLite write amplification while preserving
            # the newest BidAsk for protection decisions.
            await asyncio.sleep(0.05)
            try:
                while True:
                    item = self.execution_quote_queue.get_nowait()
                    latest[(item.symbol, item.contract)] = item
                    self.execution_quote_queue.task_done()
            except asyncio.QueueEmpty:
                pass
            try:
                for item in latest.values():
                    await asyncio.to_thread(self.execution_quote_sink.save, item)
            finally:
                self.execution_quote_queue.task_done()

    async def _run_heartbeat(self) -> None:
        while self._running:
            healthy = False
            try:
                healthy = await self.market_data_provider.heartbeat()
            except Exception:
                healthy = False
            self.last_heartbeat_time = datetime.now(TAIPEI)
            if not healthy:
                self.connection_status = "disconnected"
            self.hub.publish(self.status_message(message_type="heartbeat"))
            await asyncio.sleep(self.heartbeat_seconds)

    def status_message(self, message_type: str = "status") -> dict[str, object]:
        now = datetime.now(TAIPEI)
        latency_ms = None
        tick_age_ms = None
        bar_age_ms = None
        if self.last_tick_time and self.last_received_time:
            latency_ms = max(
                0.0,
                (self.last_received_time - self.last_tick_time).total_seconds() * 1000,
            )
            tick_age_ms = max(0.0, (now - self.last_received_time).total_seconds() * 1000)
        if self.last_bar_received_time:
            bar_age_ms = max(
                0.0,
                (now - self.last_bar_received_time).total_seconds() * 1000,
            )
        freshness_age_ms = (
            tick_age_ms if tick_age_ms is not None else bar_age_ms
        )
        if self.connection_status != "connected":
            service_status = "provider_disconnected"
        elif (
            self.connection_status == "connected"
            and freshness_age_ms is not None
            and freshness_age_ms > self.stale_after_seconds * 1_000
        ):
            service_status = "market_stale"
        elif self.dropped_ticks or self.worker_errors or self.history_error:
            service_status = "degraded"
        else:
            service_status = "healthy"
        trading_block_reason = (
            service_status
            if service_status in {"provider_disconnected", "market_stale"}
            else None
        )
        websocket = self.hub.stats()
        return {
            "type": message_type,
            "service_status": service_status,
            "market_data_provider": getattr(
                self.market_data_provider, "provider_name", "custom"
            ),
            "symbol": self.symbol,
            "contract": self.market_data_provider.contract,
            "connection_status": self.connection_status,
            "last_tick_time": isoformat_millis(self.last_tick_time)
            if self.last_tick_time else None,
            "last_bar_time": isoformat_millis(self.last_bar_time)
            if self.last_bar_time else None,
            "last_heartbeat_time": isoformat_millis(self.last_heartbeat_time)
            if self.last_heartbeat_time else None,
            "server_time": isoformat_millis(now),
            "latency_ms": round(latency_ms, 3) if latency_ms is not None else None,
            "market_latency_seconds": round(latency_ms / 1_000, 3)
            if latency_ms is not None else None,
            "tick_age_ms": round(tick_age_ms, 3) if tick_age_ms is not None else None,
            "tick_age_seconds": round(tick_age_ms / 1_000, 3)
            if tick_age_ms is not None else None,
            "bar_age_seconds": round(bar_age_ms / 1_000, 3)
            if bar_age_ms is not None else None,
            "stale_after_seconds": self.stale_after_seconds,
            "trading_block_reason": trading_block_reason,
            "queue_size": self.queue.qsize(),
            "queue_capacity": self.queue.maxsize,
            "queue_high_watermark": self.queue_high_watermark,
            "dropped_ticks": self.dropped_ticks,
            "processed_ticks": self.processed_ticks,
            "worker_cycles": self.worker_cycles,
            "worker_errors": self.worker_errors,
            "last_worker_error": self.last_worker_error,
            "average_tick_processing_ms": round(
                self.total_tick_processing_ms / self.worker_cycles, 3
            ) if self.worker_cycles else None,
            "max_tick_processing_ms": round(self.max_tick_processing_ms, 3),
            "database_write_count": self.database_write_count,
            "average_database_write_ms": round(
                self.total_database_write_ms / self.database_write_count, 3
            ) if self.database_write_count else None,
            "max_database_write_ms": round(self.max_database_write_ms, 3),
            "last_database_write_ms": round(self.last_database_write_ms, 3)
            if self.last_database_write_ms is not None else None,
            "websocket_connections": websocket["active_connections"],
            "websocket_connections_total": websocket["total_connections"],
            "websocket_disconnections_total": websocket["total_disconnections"],
            "websocket_dropped_messages": websocket["dropped_messages"],
            "duplicate_ticks": self.aggregator.duplicate_ticks,
            "late_ticks": self.aggregator.late_ticks,
            "history_bars_loaded": self.history_bars_loaded,
            "history_error": self.history_error,
        }
