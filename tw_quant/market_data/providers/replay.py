from __future__ import annotations

import asyncio
import csv
from datetime import datetime, time, timedelta
from pathlib import Path
from time import monotonic
from uuid import uuid4

from ...market import TAIPEI, KBar, TickEvent, classify_tmf_session
from ..ports import ProviderCapabilities, StatusCallback, TickCallback


def parse_exchange_time(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        result = result.replace(tzinfo=TAIPEI)
    return result.astimezone(TAIPEI)


class ReplayMarketDataProvider:
    """Replay deterministic tick CSV data without broker credentials."""

    provider_name = "replay"
    capabilities = ProviderCapabilities(live_ticks=True, historical_bars=False)

    def __init__(self, csv_path: str | Path, speed: float = 8.0, loop: bool = True):
        self.csv_path = Path(csv_path)
        self.speed = speed
        self.loop = loop
        self.symbol = ""
        self.contract = ""
        self._task: asyncio.Task | None = None
        self._healthy = False

    def load(self) -> list[dict[str, str]]:
        with self.csv_path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        required = {"exchange_time", "symbol", "contract", "price", "volume"}
        if not rows or not required.issubset(rows[0]):
            raise ValueError(
                f"replay CSV requires columns: {', '.join(sorted(required))}"
            )
        self.symbol = rows[0]["symbol"]
        self.contract = rows[0]["contract"]
        return rows

    @staticmethod
    def _live_anchor(now: datetime) -> datetime:
        local = now.astimezone(TAIPEI)
        try:
            classify_tmf_session(local)
            return local
        except ValueError:
            clock = local.time().replace(tzinfo=None)
            opening = time(8, 45) if time(5, 0) < clock < time(8, 45) else time(15, 0)
            return local.replace(
                hour=opening.hour, minute=opening.minute, second=0, microsecond=0
            )

    async def start(self, on_tick: TickCallback, on_status: StatusCallback) -> None:
        rows = self.load()
        self._healthy = True
        on_status("connected")

        async def replay() -> None:
            sequence = 0
            replay_id = uuid4().hex
            live_started_at = monotonic()
            live_exchange_anchor = self._live_anchor(datetime.now(TAIPEI))
            while self._healthy:
                previous: datetime | None = None
                for row in rows:
                    if not self._healthy:
                        return
                    source_exchange_time = parse_exchange_time(row["exchange_time"])
                    if previous is not None:
                        source_delay = max(
                            0.02,
                            min(
                                (source_exchange_time - previous).total_seconds()
                                / self.speed,
                                0.5,
                            ),
                        )
                        await asyncio.sleep(source_delay)
                    previous = source_exchange_time
                    sequence += 1
                    source_latency = float(row.get("latency_ms") or 12.0)
                    if self.loop:
                        exchange_time = live_exchange_anchor + timedelta(
                            seconds=monotonic() - live_started_at
                        )
                        event_sequence = f"replay-{replay_id}-{sequence}"
                    else:
                        exchange_time = source_exchange_time
                        event_sequence = row.get("sequence") or f"replay-{sequence}"
                    received_time = exchange_time + timedelta(milliseconds=source_latency)
                    on_tick(
                        TickEvent(
                            symbol=row["symbol"],
                            contract=row["contract"],
                            exchange_time=exchange_time,
                            received_time=received_time,
                            price=float(row["price"]),
                            volume=int(float(row["volume"])),
                            total_volume=int(float(row["total_volume"]))
                            if row.get("total_volume")
                            else None,
                            sequence=event_sequence,
                        )
                    )
                if not self.loop:
                    return
                await asyncio.sleep(0.5)

        self._task = asyncio.create_task(replay(), name="market-data-replay")

    async def stop(self) -> None:
        self._healthy = False
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def heartbeat(self) -> bool:
        return self._healthy and self._task is not None and not self._task.done()

    async def load_history(self, limit: int) -> list[KBar]:
        return []


# Backward-compatible name used by existing callers.
ReplayFeed = ReplayMarketDataProvider
