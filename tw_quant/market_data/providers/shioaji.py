from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from ...market import (
    TAIPEI,
    ExecutionQuote,
    KBar,
    TickEvent,
    classify_tmf_session,
    minute_floor,
)
from ..ports import (
    ExecutionQuoteCallback,
    ProviderCapabilities,
    StatusCallback,
    TickCallback,
)
from .replay import parse_exchange_time


class ShioajiMarketDataProvider:
    """Quote-only Shioaji adapter; it never activates a CA or places orders."""

    provider_name = "shioaji"
    capabilities = ProviderCapabilities(live_ticks=True, historical_bars=True)

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        contract: str,
        production: bool,
        history_days: int = 7,
        symbol: str = "TMF",
    ):
        self.api_key = api_key
        self.secret_key = secret_key
        self.symbol = symbol
        self.requested_contract = contract
        self.contract = contract
        self.production = production
        self.history_days = history_days
        self.api = None
        self._resolved_contract = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._on_tick: TickCallback | None = None
        self._on_status: StatusCallback | None = None
        self._connected = False
        self._on_execution_quote: ExecutionQuoteCallback | None = None
        self._last_price: float | None = None

    def set_execution_quote_callback(
        self, callback: ExecutionQuoteCallback
    ) -> None:
        self._on_execution_quote = callback

    async def start(self, on_tick: TickCallback, on_status: StatusCallback) -> None:
        self._loop = asyncio.get_running_loop()
        self._on_tick = on_tick
        self._on_status = on_status
        on_status("connecting")
        await asyncio.to_thread(self._login_and_subscribe)

    def _emit_status(self, status: str) -> None:
        self._connected = status == "connected"
        if self._loop and self._on_status:
            self._loop.call_soon_threadsafe(self._on_status, status)

    def _login_and_subscribe(self) -> None:
        try:
            import shioaji as sj
        except ImportError as exc:
            raise RuntimeError("install the shioaji optional dependency") from exc

        self.api = sj.Shioaji(simulation=not self.production)
        self.api.login(
            api_key=self.api_key,
            secret_key=self.secret_key,
            subscribe_trade=False,
        )
        contracts = getattr(self.api.Contracts.Futures, self.symbol, None)
        if contracts is None:
            raise RuntimeError(f"{self.symbol} contracts were not found")
        contract = getattr(contracts, self.requested_contract, None)
        if contract is None:
            contract = getattr(contracts, f"{self.symbol}R1", None)
        if contract is None:
            raise RuntimeError(f"{self.symbol} front-month contract was not found")
        self._resolved_contract = contract
        self.contract = getattr(contract, "target_code", None) or contract.code

        def tick_callback(_exchange, tick) -> None:
            # Provider callback boundary: normalize and enqueue only.
            if self._loop is None or self._on_tick is None:
                return
            exchange_time = tick.datetime
            if exchange_time.tzinfo is None:
                exchange_time = exchange_time.replace(tzinfo=TAIPEI)
            received_time = datetime.now(TAIPEI)
            sequence = None
            for name in ("sequence", "seqno", "tick_sn"):
                value = getattr(tick, name, None)
                if value is not None:
                    sequence = str(value)
                    break
            try:
                self._last_price = float(tick.close)
                event = TickEvent(
                    symbol=self.symbol,
                    contract=tick.code or self.contract,
                    exchange_time=exchange_time.astimezone(TAIPEI),
                    received_time=received_time,
                    price=float(tick.close),
                    volume=int(tick.volume),
                    total_volume=int(tick.total_volume),
                    sequence=sequence,
                )
            except (TypeError, ValueError):
                return
            self._loop.call_soon_threadsafe(self._on_tick, event)

        def bidask_callback(_exchange, bidask) -> None:
            # Hot path: extract the top of book, enqueue a memory update, return.
            if self._loop is None or self._on_execution_quote is None:
                return
            exchange_time = bidask.datetime
            if exchange_time.tzinfo is None:
                exchange_time = exchange_time.replace(tzinfo=TAIPEI)
            try:
                bids = tuple(getattr(bidask, "bid_price", ()) or ())
                asks = tuple(getattr(bidask, "ask_price", ()) or ())
                quote = ExecutionQuote(
                    symbol=self.symbol,
                    contract=getattr(bidask, "code", None) or self.contract,
                    best_bid=float(bids[0]) if bids and float(bids[0]) > 0 else None,
                    best_ask=float(asks[0]) if asks and float(asks[0]) > 0 else None,
                    last_price=self._last_price,
                    exchange_time=exchange_time.astimezone(TAIPEI),
                    received_at=datetime.now(TAIPEI),
                    source=self.provider_name,
                )
            except (AttributeError, TypeError, ValueError):
                return
            self._loop.call_soon_threadsafe(self._on_execution_quote, quote)

        def event_callback(_resp_code: int, event_code: int, _info: str, _event: str):
            if event_code in {0, 13, 16}:
                self._emit_status("connected")
            elif event_code == 12:
                self._emit_status("reconnecting")
            elif event_code in {1, 2}:
                self._emit_status("disconnected")

        self.api.quote.set_on_tick_fop_v1_callback(tick_callback)
        self.api.quote.set_on_bidask_fop_v1_callback(bidask_callback)
        self.api.quote.set_event_callback(event_callback)
        self.api.quote.subscribe(
            contract,
            quote_type=sj.constant.QuoteType.Tick,
            version=sj.constant.QuoteVersion.v1,
        )
        self.api.quote.subscribe(
            contract,
            quote_type=sj.constant.QuoteType.BidAsk,
            version=sj.constant.QuoteVersion.v1,
        )
        self._emit_status("connected")

    @staticmethod
    def _historical_time(value) -> datetime:
        if isinstance(value, datetime):
            result = value
            if result.tzinfo is None:
                result = result.replace(tzinfo=TAIPEI)
            return result.astimezone(TAIPEI)
        if isinstance(value, str):
            return parse_exchange_time(value)
        raw = float(value)
        if raw > 1e16:
            raw /= 1e9
        elif raw > 1e13:
            raw /= 1e6
        elif raw > 1e10:
            raw /= 1e3
        # Shioaji Kbar timestamps encode Taiwan clock fields in integer form.
        return datetime.fromtimestamp(raw, timezone.utc).replace(tzinfo=TAIPEI)

    def _load_history_sync(self, limit: int) -> list[KBar]:
        if self.api is None or self._resolved_contract is None:
            return []
        now = datetime.now(TAIPEI)
        end = now.date()
        start = end - timedelta(days=self.history_days - 1)
        payload = self.api.kbars(
            contract=self._resolved_contract,
            start=start.isoformat(),
            end=end.isoformat(),
        ).dict()
        rows = zip(
            payload.get("ts", ()),
            payload.get("Open", ()),
            payload.get("High", ()),
            payload.get("Low", ()),
            payload.get("Close", ()),
            payload.get("Volume", ()),
        )
        current_minute = minute_floor(now)
        bars: list[KBar] = []
        for timestamp, open_, high, low, close, volume in rows:
            # Shioaji labels one-minute bars by their right edge.
            bar_time = minute_floor(
                self._historical_time(timestamp) - timedelta(minutes=1)
            )
            if bar_time >= current_minute:
                continue
            open_, high, low, close = map(float, (open_, high, low, close))
            volume = int(volume)
            if (
                min(open_, high, low, close) <= 0
                or high < max(open_, close)
                or low > min(open_, close)
                or volume < 0
            ):
                continue
            try:
                session, trading_date = classify_tmf_session(bar_time)
            except ValueError:
                continue
            last_tick_time = bar_time + timedelta(seconds=59, milliseconds=999)
            bars.append(
                KBar(
                    symbol=self.symbol,
                    contract=self.contract,
                    time=bar_time,
                    open=open_,
                    high=high,
                    low=low,
                    close=close,
                    volume=volume,
                    status="closed",
                    session=session,
                    trading_date=trading_date,
                    first_tick_time=bar_time,
                    last_tick_time=last_tick_time,
                    exchange_time=last_tick_time,
                    received_time=now,
                    latency_ms=0.0,
                )
            )
        return bars[-limit:]

    async def load_history(self, limit: int) -> list[KBar]:
        return await asyncio.to_thread(self._load_history_sync, limit)

    async def stop(self) -> None:
        if self.api is None:
            return
        try:
            await asyncio.to_thread(self.api.logout)
        finally:
            self._emit_status("disconnected")

    async def heartbeat(self) -> bool:
        return self._connected and self.api is not None


# Backward-compatible name used by existing callers.
ShioajiFeed = ShioajiMarketDataProvider
