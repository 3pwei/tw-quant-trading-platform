from __future__ import annotations

import asyncio
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from functools import partial
from typing import Any, Callable, Mapping

from .events import BrokerEvent, broker_event_id
from .identity import BrokerAccountRef
from .models import BrokerOrderRequest, BrokerOrderStatus, OrderSide
from .reconciliation import (
    BrokerFillSnapshot,
    BrokerOrderSnapshot,
    BrokerPositionSnapshot,
    BrokerReconciliationSnapshot,
)
from .shioaji import ExternalOrderReport, ExternalReportStatus


_STATUS_MAP: dict[str, ExternalReportStatus] = {
    "pendingsubmit": "accepted",
    "presubmitted": "accepted",
    "submitted": "accepted",
    "partfilled": "partially_filled",
    "filled": "filled",
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "failed": "rejected",
    "inactive": "expired",
}

_CANONICAL_STATUS = {
    "accepted": BrokerOrderStatus.ACCEPTED,
    "partially_filled": BrokerOrderStatus.PARTIALLY_FILLED,
    "filled": BrokerOrderStatus.FILLED,
    "cancelled": BrokerOrderStatus.CANCELLED,
    "rejected": BrokerOrderStatus.REJECTED,
    "expired": BrokerOrderStatus.EXPIRED,
}


def _value(value: object) -> object:
    return value.value if isinstance(value, Enum) else value


def _json_value(value: object) -> object:
    raw = _value(value)
    if raw is None or isinstance(raw, (str, int, float, bool)):
        return raw
    if isinstance(raw, datetime):
        return raw.isoformat()
    if isinstance(raw, Mapping):
        return {str(key): _json_value(item) for key, item in raw.items()}
    if isinstance(raw, (list, tuple)):
        return [_json_value(item) for item in raw]
    if is_dataclass(raw):
        return _json_value(asdict(raw))
    return str(raw)


def _field(source: object, name: str, default: object = None) -> object:
    if isinstance(source, Mapping):
        return source.get(name, default)
    return getattr(source, name, default)


def _text(value: object) -> str:
    raw = _value(value)
    return "" if raw is None else str(raw)


def _timestamp(value: object, fallback: datetime) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, (int, float)):
        seconds = float(value)
        if abs(seconds) >= 1e17:
            seconds /= 1e9
        elif abs(seconds) >= 1e14:
            seconds /= 1e6
        elif abs(seconds) >= 1e11:
            seconds /= 1e3
        try:
            result = datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return fallback
    elif value:
        try:
            result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return fallback
    else:
        return fallback
    if result.tzinfo is None or result.utcoffset() is None:
        return result.replace(tzinfo=timezone.utc)
    return result


def _status_name(value: object) -> str:
    return "".join(
        character for character in _text(value).lower() if character.isalnum()
    )


def _order_id(trade: object) -> str | None:
    status = _field(trade, "status", {})
    order = _field(trade, "order", {})
    for source, keys in ((status, ("id", "order_id")), (order, ("id", "seqno"))):
        for key in keys:
            value = _field(source, key)
            if value not in (None, ""):
                return str(value)
    return None


def _contract_code(value: object) -> str:
    contract = _field(value, "contract", {})
    for source, names in (
        (contract, ("code", "symbol")),
        (value, ("code", "contract_code")),
    ):
        for name in names:
            result = _text(_field(source, name))
            if result:
                return result
    return ""


def _side(value: object) -> OrderSide:
    action = _status_name(_field(_field(value, "order", value), "action"))
    if action.endswith("buy") or action == "b":
        return "buy"
    if action.endswith("sell") or action == "s":
        return "sell"
    raise ValueError("Shioaji trade direction is unavailable")


def _deal_values(
    status: object,
    fallback_time: datetime,
) -> tuple[int, float | None, datetime | None]:
    deals = _field(status, "deals", ()) or ()
    quantity = 0
    notional = 0.0
    latest: datetime | None = None
    for deal in deals:
        deal_quantity = int(_field(deal, "quantity", _field(deal, "qty", 0)) or 0)
        deal_price = float(_field(deal, "price", 0) or 0)
        if deal_quantity <= 0 or deal_price <= 0:
            continue
        quantity += deal_quantity
        notional += deal_quantity * deal_price
        occurred_at = _timestamp(
            _field(deal, "ts", _field(deal, "timestamp")),
            fallback_time,
        )
        latest = max(latest, occurred_at) if latest else occurred_at
    return quantity, (notional / quantity if quantity else None), latest


def normalize_trade(
    trade: object, *, now: Callable[[], datetime] | None = None
) -> ExternalOrderReport:
    """Convert a Shioaji Trade into the platform's aggregate broker report."""

    clock = now or (lambda: datetime.now(timezone.utc))
    received_at = clock()
    status = _field(trade, "status", {})
    raw_status = _text(_field(status, "status"))
    canonical = _STATUS_MAP.get(_status_name(raw_status))
    if canonical is None:
        raise ValueError(f"unsupported Shioaji order status: {raw_status or '<empty>'}")
    filled_quantity, average_fill_price, deal_time = _deal_values(status, received_at)
    if canonical in {"partially_filled", "filled"} and not filled_quantity:
        raise ValueError("filled Shioaji status has no valid deals")
    message = _text(
        _field(status, "msg", _field(status, "message", _field(status, "status_code")))
    )
    return ExternalOrderReport(
        broker_order_id=_order_id(trade),
        status=canonical,
        occurred_at=deal_time or received_at,
        filled_quantity=filled_quantity,
        average_fill_price=average_fill_price,
        reason=message or f"shioaji_{_status_name(raw_status)}",
    )


def _public_mapping(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return {str(key): _value(item) for key, item in value.items()}
    if is_dataclass(value):
        return {str(key): _value(item) for key, item in asdict(value).items()}
    data = getattr(value, "model_dump", None) or getattr(value, "dict", None)
    if callable(data):
        result = data()
        if isinstance(result, Mapping):
            return {str(key): _value(item) for key, item in result.items()}
    return {"value": str(value)}


class ShioajiSimulationExecutionClient:
    """Blocking Shioaji SDK wrapper restricted to the simulation environment.

    The market-data provider is deliberately not reused here. All SDK calls run
    in a worker thread so an API/worker event loop cannot be blocked by I/O.
    """

    def __init__(
        self,
        api: object,
        sdk: object,
        *,
        account: object | None = None,
        simulation: bool,
        now: Callable[[], datetime] | None = None,
    ):
        if simulation is not True:
            raise ValueError(
                "ShioajiSimulationExecutionClient requires simulation=True"
            )
        self.api = api
        self.sdk = sdk
        self.account = account
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._trades: dict[str, object] = {}
        self._sdk_lock = asyncio.Lock()

    @classmethod
    def login(
        cls,
        api_key: str,
        secret_key: str,
        *,
        account_id: str | None = None,
    ) -> ShioajiSimulationExecutionClient:
        """Create and log in to a simulation API; this path cannot select production."""

        import shioaji as sj  # type: ignore[import-not-found]

        api = sj.Shioaji(simulation=True)
        accounts = api.login(
            api_key=api_key,
            secret_key=secret_key,
            subscribe_trade=True,
        )
        account = next(
            (
                item
                for item in accounts
                if account_id is not None
                and str(getattr(item, "account_id", "")) == account_id
            ),
            getattr(api, "futopt_account", None) if account_id is None else None,
        )
        if account_id is not None and account is None:
            raise ValueError("configured Shioaji simulation account was not returned")
        return cls(api, sj, account=account, simulation=True)

    async def _call(
        self,
        function: Callable[..., Any],
        *args: object,
        **kwargs: object,
    ) -> Any:
        async with self._sdk_lock:
            return await asyncio.to_thread(partial(function, *args, **kwargs))

    def _constant(self, group: str, name: str) -> object:
        constants = getattr(self.sdk, "constant")
        return getattr(getattr(constants, group), name)

    def _contract(self, code: str) -> object:
        contracts = getattr(self.api, "Contracts", getattr(self.api, "contracts", None))
        if contracts is None:
            raise LookupError("Shioaji contracts are unavailable")
        futures = getattr(contracts, "Futures", contracts)
        getter = getattr(futures, "get", None)
        contract = getter(code) if callable(getter) else None
        if contract is None:
            try:
                contract = futures[code]
            except (KeyError, TypeError):
                contract = None
        if contract is None:
            raise LookupError(f"Shioaji futures contract not found: {code}")
        return contract

    def _futures_order(self, request: BrokerOrderRequest) -> object:
        if request.order_type == "stop":
            raise ValueError("Shioaji simulation client does not support stop orders")
        price_type = "MKT" if request.order_type == "market" else "LMT"
        price = 0 if request.order_type == "market" else request.limit_price
        return self.sdk.FuturesOrder(
            action=self._constant("Action", "Buy" if request.side == "buy" else "Sell"),
            price=price,
            quantity=request.quantity,
            price_type=self._constant("FuturesPriceType", price_type),
            order_type=self._constant("OrderType", request.time_in_force.upper()),
            octype=self._constant("FuturesOCType", "Auto"),
            account=self.account,
        )

    def _remember(self, trade: object) -> None:
        broker_order_id = _order_id(trade)
        if broker_order_id:
            self._trades[broker_order_id] = trade

    async def account_state(self) -> Mapping[str, object]:
        return {
            "account_id": getattr(self.account, "account_id", None),
            "simulation": True,
            "connected": True,
        }

    async def positions(self) -> list[Mapping[str, object]]:
        positions = await self._call(self.api.list_positions, self.account)
        return [_public_mapping(position) for position in positions]

    async def submit(self, request: BrokerOrderRequest) -> ExternalOrderReport:
        trade = await self._call(
            self.api.place_order,
            self._contract(request.contract),
            self._futures_order(request),
        )
        self._remember(trade)
        return normalize_trade(trade, now=self.now)

    async def cancel(self, broker_order_id: str) -> ExternalOrderReport:
        trade = await self._find_trade(broker_order_id)
        if trade is None:
            raise LookupError(f"Shioaji order not found: {broker_order_id}")
        await self._call(self.api.update_status, self.account, trade=trade)
        await self._call(self.api.cancel_order, trade)
        await self._call(self.api.update_status, self.account, trade=trade)
        self._remember(trade)
        return normalize_trade(trade, now=self.now)

    async def _refresh_trades(self) -> list[object]:
        await self._call(self.api.update_status, self.account)
        trades = list(await self._call(self.api.list_trades))
        for trade in trades:
            self._remember(trade)
        return trades

    async def _find_trade(self, broker_order_id: str) -> object | None:
        trade = self._trades.get(broker_order_id)
        if trade is not None:
            return trade
        await self._refresh_trades()
        return self._trades.get(broker_order_id)

    async def order(self, broker_order_id: str) -> ExternalOrderReport | None:
        await self._refresh_trades()
        trade = self._trades.get(broker_order_id)
        return normalize_trade(trade, now=self.now) if trade is not None else None

    async def order_by_client_id(
        self, client_order_id: str
    ) -> ExternalOrderReport | None:
        # FuturesOrder does not document a durable client-order-id field. Guessing
        # after an ambiguous submission could duplicate a real order, so fail closed.
        return None

    def _raw_reconciliation_snapshot(self) -> tuple[list[object], list[object]]:
        self.api.update_status(self.account)
        trades = list(self.api.list_trades())
        positions = list(self.api.list_positions(self.account))
        return trades, positions

    async def reconciliation_snapshot(self) -> BrokerReconciliationSnapshot:
        """Read orders, deals, and positions as one serialized SDK operation."""

        captured_at = self.now()
        trades, raw_positions = await self._call(self._raw_reconciliation_snapshot)
        orders: list[BrokerOrderSnapshot] = []
        fills: list[BrokerFillSnapshot] = []
        for trade_index, trade in enumerate(trades):
            self._remember(trade)
            report = normalize_trade(trade, now=lambda: captured_at)
            orders.append(BrokerOrderSnapshot(
                broker_order_id=report.broker_order_id,
                status=_CANONICAL_STATUS[report.status],
                filled_quantity=report.filled_quantity,
            ))
            status = _field(trade, "status", {})
            contract = _contract_code(trade)
            side = _side(trade)
            for deal_index, deal in enumerate(_field(status, "deals", ()) or ()):
                if not report.broker_order_id:
                    raise ValueError("Shioaji deal has no broker order ID")
                deal_quantity = int(
                    _field(deal, "quantity", _field(deal, "qty", 0)) or 0
                )
                deal_price = float(_field(deal, "price", 0) or 0)
                deal_time = _timestamp(
                    _field(deal, "ts", _field(deal, "timestamp")),
                    captured_at,
                )
                raw_fill_id = _field(
                    deal,
                    "trade_id",
                    _field(deal, "seq", f"{trade_index}-{deal_index}"),
                )
                fills.append(BrokerFillSnapshot(
                    fill_id=f"{report.broker_order_id}:{raw_fill_id}",
                    broker_order_id=report.broker_order_id,
                    contract=contract,
                    side=side,
                    quantity=deal_quantity,
                    price=deal_price,
                    occurred_at=deal_time,
                ))
        positions: list[BrokerPositionSnapshot] = []
        for position in raw_positions:
            contract = _contract_code(position)
            quantity = int(_field(position, "quantity", 0) or 0)
            direction = _status_name(
                _field(position, "direction", _field(position, "action"))
            )
            if direction.endswith("sell") or direction == "s":
                quantity = -abs(quantity)
            elif direction.endswith("buy") or direction == "b":
                quantity = abs(quantity)
            elif quantity > 0:
                raise ValueError("Shioaji position direction is unavailable")
            positions.append(BrokerPositionSnapshot(contract, quantity))
        account_id = _text(_field(self.account, "account_id"))
        return BrokerReconciliationSnapshot(
            broker_name="shioaji",
            account_id=account_id,
            captured_at=captured_at,
            orders=tuple(orders),
            fills=tuple(fills),
            positions=tuple(positions),
        )


ShioajiCallbackEvent = BrokerEvent


def normalize_callback(
    state: object,
    message: object,
    *,
    account_ref: BrokerAccountRef,
    now: Callable[[], datetime] | None = None,
) -> ShioajiCallbackEvent:
    """Validate a FORDER/FDEAL callback without mutating persistence."""

    event_type = _text(state).upper()
    if event_type not in {"FORDER", "FDEAL"}:
        raise ValueError(
            f"unsupported Shioaji callback state: {event_type or '<empty>'}"
        )
    if not isinstance(message, Mapping):
        raise ValueError("Shioaji callback message must be a mapping")
    clock = now or (lambda: datetime.now(timezone.utc))
    payload = {str(key): _json_value(value) for key, value in message.items()}
    candidates = (
        payload.get("id"),
        payload.get("order_id"),
        payload.get("ordno"),
    )
    broker_order_id = next((str(value) for value in candidates if value), None)
    return BrokerEvent(
        event_id=broker_event_id(
            account_ref.broker_name,
            account_ref.account_id,
            event_type,
            broker_order_id,
            payload,
        ),
        broker_name="shioaji",
        account_id=account_ref.account_id,
        event_type=event_type,
        broker_order_id=broker_order_id,
        received_at=clock(),
        payload=payload,
    )


class ShioajiCallbackBridge:
    """Thread-safe callback edge; consumers own reconciliation and persistence."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        account_ref: BrokerAccountRef,
        queue: asyncio.Queue[ShioajiCallbackEvent] | None = None,
        *,
        enqueue_callback: Callable[[ShioajiCallbackEvent], bool] | None = None,
        now: Callable[[], datetime] | None = None,
    ):
        if (queue is None) == (enqueue_callback is None):
            raise ValueError("provide exactly one callback queue or callback sink")
        self.loop = loop
        if account_ref.broker_name != "shioaji":
            raise ValueError("Shioaji callback bridge requires a Shioaji account")
        self.account_ref = account_ref
        self.queue = queue
        self.enqueue_callback = enqueue_callback
        self.now = now

    def __call__(self, state: object, message: object) -> None:
        try:
            event = normalize_callback(
                state, message, account_ref=self.account_ref, now=self.now
            )
        except (TypeError, ValueError):
            return
        self.loop.call_soon_threadsafe(self._enqueue, event)

    def _enqueue(self, event: ShioajiCallbackEvent) -> None:
        if self.enqueue_callback is not None:
            self.enqueue_callback(event)
            return
        assert self.queue is not None
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            # Dropped callbacks are recovered by periodic broker reconciliation.
            return
