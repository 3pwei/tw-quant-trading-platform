from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

from .live.service import LiveMarketService
from .live.storage import SQLiteBarRepository
from .market import TAIPEI, TickEvent


@dataclass(frozen=True)
class SoakThresholds:
    max_callback_ms: float = 10.0
    max_queue_depth: int = 500
    max_average_tick_ms: float = 50.0
    max_tick_ms: float = 1_000.0
    max_average_database_write_ms: float = 20.0
    max_database_write_ms: float = 500.0
    max_market_latency_seconds: float = 1.0


@dataclass(frozen=True)
class SoakReport:
    passed: bool
    duration_seconds: float
    emitted_ticks: int
    processed_ticks: int
    dropped_ticks: int
    final_queue_depth: int
    queue_high_watermark: int
    max_callback_ms: float
    average_tick_processing_ms: float | None
    max_tick_processing_ms: float
    average_database_write_ms: float | None
    max_database_write_ms: float
    market_latency_seconds: float | None
    failures: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["failures"] = list(self.failures)
        return result


class SyntheticSoakProvider:
    """Generate live-shaped ticks while measuring the callback boundary."""

    provider_name = "level2-soak"
    contract = "TMFSOAK"

    def __init__(self, tick_interval_seconds: float) -> None:
        if tick_interval_seconds <= 0:
            raise ValueError("tick_interval_seconds must be positive")
        self.tick_interval_seconds = tick_interval_seconds
        self.emitted_ticks = 0
        self.max_callback_ms = 0.0
        self._healthy = False
        self._task: asyncio.Task | None = None

    async def start(self, on_tick, on_status) -> None:
        self._healthy = True
        on_status("connected")
        # A deterministic night-session anchor lets a multi-hour synthetic run
        # execute at any wall-clock time without crossing the 05:00 session end.
        anchor = datetime.now(TAIPEI).replace(
            hour=15, minute=0, second=0, microsecond=0
        )

        async def produce() -> None:
            started_at = perf_counter()
            while self._healthy:
                elapsed = perf_counter() - started_at
                exchange_time = anchor + timedelta(seconds=elapsed)
                received_time = exchange_time + timedelta(milliseconds=5)
                sequence = self.emitted_ticks + 1
                tick = TickEvent(
                    symbol="TMF",
                    contract=self.contract,
                    exchange_time=exchange_time,
                    received_time=received_time,
                    price=20_000 + sequence % 20,
                    volume=1,
                    total_volume=sequence,
                    sequence=f"soak-{sequence}",
                )
                callback_started = perf_counter()
                on_tick(tick)
                self.max_callback_ms = max(
                    self.max_callback_ms,
                    (perf_counter() - callback_started) * 1_000,
                )
                self.emitted_ticks = sequence
                await asyncio.sleep(self.tick_interval_seconds)

        self._task = asyncio.create_task(produce(), name="level2-soak-provider")

    async def stop(self) -> None:
        self._healthy = False
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def heartbeat(self) -> bool:
        return self._healthy and self._task is not None and not self._task.done()


async def run_level2_soak(
    duration_seconds: float,
    *,
    tick_interval_seconds: float = 0.1,
    database_path: str | Path | None = None,
    thresholds: SoakThresholds = SoakThresholds(),
) -> SoakReport:
    if duration_seconds <= 0:
        raise ValueError("duration_seconds must be positive")

    temporary: TemporaryDirectory[str] | None = None
    if database_path is None:
        temporary = TemporaryDirectory()
        database_path = Path(temporary.name) / "level2-soak.sqlite3"
    repository = SQLiteBarRepository(database_path)
    provider = SyntheticSoakProvider(tick_interval_seconds)
    service = LiveMarketService(
        provider,
        repository,
        heartbeat_seconds=min(1.0, max(0.05, tick_interval_seconds * 2)),
    )
    started = perf_counter()
    try:
        await service.start()
        await asyncio.sleep(duration_seconds)
        await provider.stop()
        await asyncio.wait_for(
            service.queue.join(), timeout=max(5.0, duration_seconds)
        )
        elapsed = perf_counter() - started
        status = service.status_message()
        failures: list[str] = []

        def limit(name: str, actual: float | int | None, maximum: float | int) -> None:
            if actual is None or actual > maximum:
                failures.append(f"{name}={actual!r} exceeds {maximum}")

        if int(status["dropped_ticks"]) != 0:
            failures.append(f"dropped_ticks={status['dropped_ticks']} must be 0")
        if service.queue.qsize() != 0:
            failures.append(f"final_queue_depth={service.queue.qsize()} must be 0")
        if int(status["processed_ticks"]) != provider.emitted_ticks:
            failures.append(
                "processed_ticks does not match emitted_ticks: "
                f"{status['processed_ticks']} != {provider.emitted_ticks}"
            )
        limit("max_callback_ms", provider.max_callback_ms, thresholds.max_callback_ms)
        limit(
            "queue_high_watermark",
            int(status["queue_high_watermark"]),
            thresholds.max_queue_depth,
        )
        limit(
            "average_tick_processing_ms",
            status["average_tick_processing_ms"],
            thresholds.max_average_tick_ms,
        )
        limit(
            "max_tick_processing_ms",
            status["max_tick_processing_ms"],
            thresholds.max_tick_ms,
        )
        limit(
            "average_database_write_ms",
            status["average_database_write_ms"],
            thresholds.max_average_database_write_ms,
        )
        limit(
            "max_database_write_ms",
            status["max_database_write_ms"],
            thresholds.max_database_write_ms,
        )
        limit(
            "market_latency_seconds",
            status["market_latency_seconds"],
            thresholds.max_market_latency_seconds,
        )
        return SoakReport(
            passed=not failures,
            duration_seconds=round(elapsed, 3),
            emitted_ticks=provider.emitted_ticks,
            processed_ticks=int(status["processed_ticks"]),
            dropped_ticks=int(status["dropped_ticks"]),
            final_queue_depth=service.queue.qsize(),
            queue_high_watermark=int(status["queue_high_watermark"]),
            max_callback_ms=round(provider.max_callback_ms, 3),
            average_tick_processing_ms=status["average_tick_processing_ms"],
            max_tick_processing_ms=float(status["max_tick_processing_ms"]),
            average_database_write_ms=status["average_database_write_ms"],
            max_database_write_ms=float(status["max_database_write_ms"]),
            market_latency_seconds=status["market_latency_seconds"],
            failures=tuple(failures),
        )
    finally:
        await service.stop()
        repository.close()
        if temporary:
            temporary.cleanup()
