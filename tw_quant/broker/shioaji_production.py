from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from functools import partial
import json
from pathlib import Path
import threading
from time import perf_counter
from typing import Any, Callable, Literal, Mapping

from .capabilities import BrokerCapabilities
from .events import BrokerEvent, broker_event_id
from .identity import BrokerAccountRef
from .instruments import BrokerInstrumentMapper, CanonicalInstrument
from .models import BrokerOrderRequest, BrokerOrderStatus, OrderSide
from .reconciliation import (
    BrokerFillSnapshot,
    BrokerOrderSnapshot,
    BrokerPositionSnapshot,
    BrokerReconciliationSnapshot,
)
from .secrets import BrokerSecretMaterial
from .shioaji import ExternalOrderReport


READ_ONLY_ERROR = "Shioaji production execution is read-only"

SHIOAJI_TECHNICAL_CAPABILITIES = BrokerCapabilities(
    supports_market_orders=True,
    supports_limit_orders=True,
    supports_cancel=True,
    supports_order_callback=True,
    supports_fill_callback=True,
    supports_partial_fills=True,
)

# Runtime capabilities, not theoretical SDK features. No write capability is
# advertised until a later, separately reviewed production-order PR.
SHIOAJI_READ_ONLY_CAPABILITIES = BrokerCapabilities(
    supports_order_callback=True,
    supports_fill_callback=True,
    supports_partial_fills=True,
)

SHIOAJI_CANARY_CAPABILITIES = BrokerCapabilities(
    supports_market_orders=True,
    supports_limit_orders=True,
    supports_ioc=True,
    supports_cancel=True,
    supports_order_callback=True,
    supports_fill_callback=True,
    supports_partial_fills=True,
)

_STATUS_MAP = {
    "pendingsubmit": BrokerOrderStatus.ACCEPTED,
    "presubmitted": BrokerOrderStatus.ACCEPTED,
    "submitted": BrokerOrderStatus.ACCEPTED,
    "partfilled": BrokerOrderStatus.PARTIALLY_FILLED,
    "filled": BrokerOrderStatus.FILLED,
    "cancelled": BrokerOrderStatus.CANCELLED,
    "canceled": BrokerOrderStatus.CANCELLED,
    "failed": BrokerOrderStatus.REJECTED,
    "rejected": BrokerOrderStatus.REJECTED,
    "inactive": BrokerOrderStatus.EXPIRED,
    "expired": BrokerOrderStatus.EXPIRED,
}


class ShioajiProductionError(RuntimeError):
    """Sanitized, stable operational failure from the production boundary."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class ShioajiInstrumentMapper(BrokerInstrumentMapper):
    """Explicit allowlisted mapping; unknown contracts never fall back to identity."""

    def __init__(self, mappings: Mapping[CanonicalInstrument, str]):
        self._to_broker = dict(mappings)
        self._to_canonical: dict[str, CanonicalInstrument] = {}
        for instrument, broker_contract in self._to_broker.items():
            code = broker_contract.strip()
            if not code or code in self._to_canonical:
                raise ValueError("Shioaji instrument mapping must be one-to-one")
            self._to_canonical[code] = instrument

    @classmethod
    def from_json(cls, value: str) -> "ShioajiInstrumentMapper":
        """Build an explicit mapping from broker-neutral deployment metadata."""

        try:
            records = json.loads(value)
            if not isinstance(records, list) or not records:
                raise ValueError
            mappings = {
                CanonicalInstrument(
                    str(record["symbol"]), str(record["contract"])
                ): str(record["broker_contract"])
                for record in records
                if isinstance(record, Mapping)
            }
            if len(mappings) != len(records):
                raise ValueError
            return cls(mappings)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("invalid broker instrument mapping") from exc

    def to_broker_contract(self, instrument: CanonicalInstrument) -> str:
        try:
            return self._to_broker[instrument]
        except KeyError as exc:
            raise LookupError("unknown canonical Shioaji instrument") from exc

    def to_canonical_instrument(self, broker_contract: str) -> CanonicalInstrument:
        try:
            return self._to_canonical[broker_contract.strip()]
        except KeyError as exc:
            raise LookupError("unknown Shioaji broker contract") from exc


def _field(value: object, name: str, default: object = None) -> object:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _value(value: object) -> object:
    return value.value if isinstance(value, Enum) else value


def _text(value: object) -> str:
    raw = _value(value)
    return "" if raw is None else str(raw)


def _name(value: object) -> str:
    return "".join(character for character in _text(value).lower() if character.isalnum())


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
        except (OSError, OverflowError, ValueError) as exc:
            raise ShioajiProductionError("broker_fill_normalization_failed") from exc
    elif value:
        try:
            result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ShioajiProductionError("broker_fill_normalization_failed") from exc
    else:
        result = fallback
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result


def _contract_code(value: object) -> str:
    contract = _field(value, "contract", {})
    return _text(
        _field(contract, "code", _field(contract, "symbol", _field(value, "code", "")))
    )


def _order_id(trade: object) -> str | None:
    for source, keys in (
        (_field(trade, "status", {}), ("id", "order_id", "ordno")),
        (_field(trade, "order", {}), ("id", "seqno", "ordno")),
    ):
        for key in keys:
            value = _field(source, key)
            if value not in (None, ""):
                return str(value)
    return None


def _side(value: object) -> OrderSide:
    action = _name(_field(_field(value, "order", value), "action"))
    if action.endswith("buy") or action == "b":
        return "buy"
    if action.endswith("sell") or action == "s":
        return "sell"
    raise ShioajiProductionError("broker_fill_normalization_failed")


def _status(trade: object) -> BrokerOrderStatus:
    raw = _name(_field(_field(trade, "status", {}), "status"))
    try:
        return _STATUS_MAP[raw]
    except KeyError as exc:
        raise ShioajiProductionError("broker_status_normalization_failed") from exc


def _safe_mapping(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return {str(key): _value(item) for key, item in value.items()}
    if is_dataclass(value):
        return {str(key): _value(item) for key, item in asdict(value).items()}
    dump = getattr(value, "model_dump", None) or getattr(value, "dict", None)
    result = dump() if callable(dump) else {}
    return result if isinstance(result, Mapping) else {}


_CALLBACK_FIELDS = frozenset({
    "id", "order_id", "ordno", "seqno", "trade_id", "exchange_seq",
    "account_id", "action", "code", "status", "price", "quantity", "qty", "ts",
})


def normalize_production_callback(
    state: object,
    message: object,
    *,
    account_ref: BrokerAccountRef,
    now: Callable[[], datetime] | None = None,
) -> BrokerEvent:
    event_type = _text(state).upper()
    if event_type not in {"FORDER", "FDEAL"}:
        raise ShioajiProductionError("callback_normalization_failed")
    raw = _safe_mapping(message)
    payload = {
        str(key): _value(value)
        for key, value in raw.items()
        if str(key) in _CALLBACK_FIELDS
        and isinstance(_value(value), (str, int, float, bool, type(None)))
    }
    callback_account = _text(payload.get("account_id"))
    if callback_account and callback_account != account_ref.account_id:
        raise ShioajiProductionError("callback_account_mismatch")
    broker_order_id = next(
        (str(payload[key]) for key in ("id", "order_id", "ordno") if payload.get(key)),
        None,
    )
    received_at = (now or (lambda: datetime.now(timezone.utc)))()
    return BrokerEvent(
        event_id=broker_event_id(
            account_ref.broker_name,
            account_ref.account_id,
            event_type,
            broker_order_id,
            payload,
        ),
        broker_name=account_ref.broker_name,
        account_id=account_ref.account_id,
        event_type=event_type,
        broker_order_id=broker_order_id,
        received_at=received_at,
        payload=payload,
    )


@dataclass(frozen=True)
class ShioajiCallbackMetrics:
    received: int
    dropped: int
    normalization_failed: int
    queue_size: int
    queue_high_watermark: int
    last_callback_time: datetime | None


class ShioajiProductionCallbackBridge:
    """Non-blocking SDK callback edge with bounded-loss observability."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        account_ref: BrokerAccountRef,
        queue: asyncio.Queue[BrokerEvent],
        *,
        now: Callable[[], datetime] | None = None,
        on_degraded: Callable[[], None] | None = None,
    ):
        if queue.maxsize <= 0:
            raise ValueError("production callback queue must be bounded")
        if account_ref.broker_name != "shioaji":
            raise ValueError("Shioaji callback requires a Shioaji account")
        self.loop = loop
        self.account_ref = account_ref
        self.queue = queue
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.on_degraded = on_degraded
        self._received = self._dropped = self._normalization_failed = 0
        self._high_watermark = 0
        self._last_callback_time: datetime | None = None
        self._metrics_lock = threading.Lock()

    def __call__(self, state: object, message: object) -> None:
        timestamp = self.now()
        with self._metrics_lock:
            self._received += 1
            self._last_callback_time = timestamp
        try:
            event = normalize_production_callback(
                state, message, account_ref=self.account_ref, now=lambda: timestamp
            )
        except (TypeError, ValueError, ShioajiProductionError):
            with self._metrics_lock:
                self._normalization_failed += 1
            if self.on_degraded is not None:
                self.on_degraded()
            return
        self.loop.call_soon_threadsafe(self._enqueue, event)

    def _enqueue(self, event: BrokerEvent) -> None:
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            with self._metrics_lock:
                self._dropped += 1
            if self.on_degraded is not None:
                self.on_degraded()
            return
        with self._metrics_lock:
            self._high_watermark = max(self._high_watermark, self.queue.qsize())
    def metrics(self) -> ShioajiCallbackMetrics:
        with self._metrics_lock:
            return ShioajiCallbackMetrics(
                received=self._received,
                dropped=self._dropped,
                normalization_failed=self._normalization_failed,
                queue_size=self.queue.qsize(),
                queue_high_watermark=self._high_watermark,
                last_callback_time=self._last_callback_time,
            )


class ShioajiProductionExecutionClient:
    """Independent production SDK client exposing read-only broker truth."""

    def __init__(
        self,
        *,
        account_ref: BrokerAccountRef,
        allowed_accounts: frozenset[BrokerAccountRef],
        secret_material: BrokerSecretMaterial,
        instrument_mapper: ShioajiInstrumentMapper,
        sdk: object | None = None,
        callback_queue_size: int = 1024,
        effective_mode: Literal["read_only", "canary"] = "read_only",
        now: Callable[[], datetime] | None = None,
    ):
        if account_ref.broker_name != "shioaji":
            raise ValueError("production client requires a Shioaji account")
        if callback_queue_size <= 0:
            raise ValueError("callback_queue_size must be positive")
        self.account_ref = account_ref
        self.allowed_accounts = allowed_accounts
        self.secret_material = secret_material
        self.instrument_mapper = instrument_mapper
        self.sdk = sdk
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.effective_mode = effective_mode
        self.api: object | None = None
        self.account: object | None = None
        self.callback_queue: asyncio.Queue[BrokerEvent] = asyncio.Queue(callback_queue_size)
        self.callback_bridge: ShioajiProductionCallbackBridge | None = None
        self._sdk_lock = asyncio.Lock()
        self._state = "disconnected"
        self._connected = self._ca_ready = self._callback_registered = False
        self._last_broker_read_time: datetime | None = None
        self._trades: dict[str, object] = {}
        self._external_order_calls = 0
        self._external_cancel_calls = 0
        self._broker_response_total_ms = 0.0
        self._broker_response_max_ms = 0.0

    @property
    def state(self) -> str:
        return self._state

    def _secrets(self) -> tuple[str, str, str, Path]:
        values = dict(self.secret_material.values)
        try:
            ca_path = self.secret_material.files[0]
            return values["api_key"], values["secret_key"], values["ca_password"], ca_path
        except (IndexError, KeyError) as exc:
            raise ShioajiProductionError("ca_secret_missing") from exc

    async def _call(self, function: Callable[..., Any], *args: object, **kwargs: object) -> Any:
        async with self._sdk_lock:
            return await asyncio.to_thread(partial(function, *args, **kwargs))

    async def start(self) -> None:
        ready_state = "canary_ready" if self.effective_mode == "canary" else "read_only_ready"
        if self._state == ready_state:
            return
        self._state = "connecting"
        try:
            api_key, secret_key, ca_password, ca_path = self._secrets()
            if not ca_path.is_file():
                raise ShioajiProductionError("ca_secret_missing")
            if self.account_ref not in self.allowed_accounts:
                raise ShioajiProductionError("account_not_allowed")
            sdk = self.sdk
            if sdk is None:
                import shioaji as sdk  # type: ignore[import-not-found,no-redef]
                self.sdk = sdk
            try:
                self.api = await asyncio.to_thread(sdk.Shioaji, simulation=False)
            except Exception as exc:
                raise ShioajiProductionError("broker_login_failed") from exc
            try:
                await self._call(
                    self.api.login,
                    api_key=api_key,
                    secret_key=secret_key,
                    subscribe_trade=False,
                )
            except Exception as exc:
                raise ShioajiProductionError("broker_login_failed") from exc
            self._connected = True
            try:
                accounts = list(await self._call(self.api.list_accounts))
            except Exception as exc:
                raise ShioajiProductionError("broker_login_failed") from exc
            matches = [
                account for account in accounts
                if _text(_field(account, "account_id")) == self.account_ref.account_id
                and _name(_field(account, "account_type", "F")) in {"f", "future", "futures"}
            ]
            if not matches:
                raise ShioajiProductionError("configured_account_not_found")
            if len(matches) != 1:
                raise ShioajiProductionError("account_identity_ambiguous")
            self.account = matches[0]
            person_id = _text(_field(self.account, "person_id"))
            try:
                activated = await self._call(
                    self.api.activate_ca,
                    ca_path=str(ca_path),
                    ca_passwd=ca_password,
                    person_id=person_id,
                )
            except Exception as exc:
                raise ShioajiProductionError("ca_activation_failed") from exc
            if activated is not True:
                raise ShioajiProductionError("ca_activation_failed")
            self._ca_ready = True
            loop = asyncio.get_running_loop()
            self.callback_bridge = ShioajiProductionCallbackBridge(
                loop,
                self.account_ref,
                self.callback_queue,
                now=self.now,
                on_degraded=self._mark_degraded,
            )
            try:
                await self._call(self.api.set_order_callback, self.callback_bridge)
                subscribe = getattr(self.api, "subscribe_trade", None)
                if callable(subscribe):
                    await self._call(subscribe, self.account)
            except Exception as exc:
                raise ShioajiProductionError("callback_registration_failed") from exc
            self._callback_registered = True
            self._state = ready_state
        except ShioajiProductionError:
            self._state = "locked"
            await self._logout_after_failed_start()
            raise
        except Exception as exc:
            self._state = "locked"
            await self._logout_after_failed_start()
            raise ShioajiProductionError("broker_login_failed") from exc

    def _mark_degraded(self) -> None:
        if self._state in {"read_only_ready", "canary_ready"}:
            self._state = "degraded"

    async def _logout_after_failed_start(self) -> None:
        api = self.api
        if api is not None and callable(getattr(api, "logout", None)):
            try:
                await self._call(api.logout)
            except Exception:
                pass
        self._connected = self._ca_ready = self._callback_registered = False

    async def close(self) -> None:
        api = self.api
        if api is not None and callable(getattr(api, "logout", None)):
            try:
                await self._call(api.logout)
            except Exception:
                pass
        self._connected = self._ca_ready = self._callback_registered = False
        self._state = "disconnected"

    async def account_state(self) -> Mapping[str, object]:
        return {
            "broker_name": self.account_ref.broker_name,
            "account_id": self.account_ref.account_id,
            "connected": self._connected,
            "ca_ready": self._ca_ready,
            "read_only": self.effective_mode == "read_only",
            "canary": self.effective_mode == "canary",
        }

    def health_state(self) -> Mapping[str, object]:
        metrics = self.callback_bridge.metrics() if self.callback_bridge else None
        return {
            "execution_state": self._state,
            "connected": self._connected,
            "ca_ready": self._ca_ready,
            "read_only": self.effective_mode == "read_only",
            "canary": self.effective_mode == "canary",
            "callback_registered": self._callback_registered,
            "last_broker_read_time": (
                self._last_broker_read_time.isoformat() if self._last_broker_read_time else None
            ),
            "last_callback_time": (
                metrics.last_callback_time.isoformat()
                if metrics and metrics.last_callback_time else None
            ),
            "callbacks_received_total": metrics.received if metrics else 0,
            "callbacks_dropped_total": metrics.dropped if metrics else 0,
            "callback_normalization_failed_total": (
                metrics.normalization_failed if metrics else 0
            ),
            "callback_queue_high_watermark": (
                metrics.queue_high_watermark if metrics else 0
            ),
            "external_order_calls": self._external_order_calls,
            "external_cancel_calls": self._external_cancel_calls,
            "average_broker_response_ms": (
                self._broker_response_total_ms
                / (self._external_order_calls + self._external_cancel_calls)
                if self._external_order_calls + self._external_cancel_calls else None
            ),
            "max_broker_response_ms": self._broker_response_max_ms,
        }

    def _require_ready(self) -> tuple[object, object]:
        if (
            self._state not in {"read_only_ready", "canary_ready", "degraded"}
            or self.api is None
            or self.account is None
        ):
            raise ShioajiProductionError("broker_read_failed")
        return self.api, self.account

    def _raw_snapshot(self) -> tuple[list[object], list[object]]:
        api, account = self._require_ready()
        api.update_status(account)
        return list(api.list_trades()), list(api.list_positions(account))

    def _assert_trade_account(self, trade: object) -> None:
        order = _field(trade, "order", {})
        account_id = _text(_field(order, "account_id", _field(trade, "account_id", "")))
        if account_id != self.account_ref.account_id:
            raise ShioajiProductionError("broker_read_failed")

    def _normalize_snapshot(
        self, trades: list[object], raw_positions: list[object], captured_at: datetime
    ) -> BrokerReconciliationSnapshot:
        orders: list[BrokerOrderSnapshot] = []
        fills: list[BrokerFillSnapshot] = []
        for trade in trades:
            self._assert_trade_account(trade)
            broker_order_id = _order_id(trade)
            status = _status(trade)
            deals = _field(_field(trade, "status", {}), "deals", ()) or ()
            filled_quantity = sum(
                int(_field(deal, "quantity", _field(deal, "qty", 0)) or 0)
                for deal in deals
            )
            if status in {
                BrokerOrderStatus.PARTIALLY_FILLED,
                BrokerOrderStatus.FILLED,
            } and filled_quantity <= 0:
                raise ShioajiProductionError("broker_fill_normalization_failed")
            orders.append(BrokerOrderSnapshot(broker_order_id, status, filled_quantity))
            canonical = self.instrument_mapper.to_canonical_instrument(_contract_code(trade))
            side = _side(trade)
            for deal in deals:
                if not broker_order_id:
                    raise ShioajiProductionError("broker_fill_normalization_failed")
                durable_id = next(
                    (
                        _field(deal, key)
                        for key in ("trade_id", "exchange_seq", "seq")
                        if _field(deal, key) not in (None, "")
                    ),
                    None,
                )
                if durable_id is None:
                    raise ShioajiProductionError("broker_fill_normalization_failed")
                fills.append(BrokerFillSnapshot(
                    fill_id=f"{broker_order_id}:{durable_id}",
                    broker_order_id=broker_order_id,
                    contract=canonical.contract,
                    side=side,
                    quantity=int(_field(deal, "quantity", _field(deal, "qty", 0)) or 0),
                    price=float(_field(deal, "price", 0) or 0),
                    occurred_at=_timestamp(
                        _field(deal, "ts", _field(deal, "timestamp")),
                        captured_at,
                    ),
                ))
        net: defaultdict[str, int] = defaultdict(int)
        for position in raw_positions:
            try:
                canonical = self.instrument_mapper.to_canonical_instrument(
                    _contract_code(position)
                )
                quantity = int(_field(position, "quantity", 0) or 0)
                direction = _name(_field(position, "direction", _field(position, "action")))
                if direction.endswith("buy") or direction == "b":
                    net[canonical.contract] += abs(quantity)
                elif direction.endswith("sell") or direction == "s":
                    net[canonical.contract] -= abs(quantity)
                elif quantity:
                    raise ValueError
            except (LookupError, TypeError, ValueError) as exc:
                raise ShioajiProductionError("broker_position_normalization_failed") from exc
        positions = tuple(
            BrokerPositionSnapshot(contract, net[contract])
            for contract in sorted(net)
        )
        return BrokerReconciliationSnapshot(
            broker_name=self.account_ref.broker_name,
            account_id=self.account_ref.account_id,
            captured_at=captured_at,
            orders=tuple(orders), fills=tuple(fills), positions=positions,
        )

    async def reconciliation_snapshot(self) -> BrokerReconciliationSnapshot:
        captured_at = self.now()
        try:
            trades, positions = await self._call(self._raw_snapshot)
            snapshot = self._normalize_snapshot(trades, positions, captured_at)
        except ShioajiProductionError:
            self._state = "degraded"
            raise
        except Exception as exc:
            self._state = "degraded"
            raise ShioajiProductionError("broker_read_failed") from exc
        self._last_broker_read_time = captured_at
        self._state = (
            "canary_ready" if self.effective_mode == "canary" else "read_only_ready"
        )
        return snapshot

    async def positions(self) -> list[Mapping[str, object]]:
        snapshot = await self.reconciliation_snapshot()
        return [
            {"contract": item.contract, "quantity": item.quantity}
            for item in snapshot.positions
        ]

    async def order(self, broker_order_id: str) -> ExternalOrderReport | None:
        snapshot = await self.reconciliation_snapshot()
        match = next(
            (
                item
                for item in snapshot.orders
                if item.broker_order_id == broker_order_id
            ),
            None,
        )
        if match is None:
            return None
        return ExternalOrderReport(
            broker_order_id=match.broker_order_id,
            status=match.status.value,  # type: ignore[arg-type]
            occurred_at=snapshot.captured_at,
            filled_quantity=match.filled_quantity,
            average_fill_price=(
                sum(
                    fill.price * fill.quantity
                    for fill in snapshot.fills
                    if fill.broker_order_id == broker_order_id
                ) / match.filled_quantity
                if match.filled_quantity else None
            ),
            reason="shioaji_broker_truth",
        )

    async def order_by_client_id(self, client_order_id: str) -> ExternalOrderReport | None:
        return None

    async def submit(self, request: BrokerOrderRequest) -> ExternalOrderReport:
        if self.effective_mode != "canary":
            raise RuntimeError(READ_ONLY_ERROR)
        manual = request.source == "manual_live_canary" and bool(request.arm_id)
        guardian = (
            request.source == "live_position_guardian"
            and request.reduce_only
            and request.purpose in {"exit", "liquidation"}
        )
        if not (manual or guardian):
            raise ShioajiProductionError("live_canary_source_not_allowed")
        api, account = self._require_ready()
        if request.time_in_force != "ioc" or request.order_type not in {"limit", "market"}:
            raise ShioajiProductionError("live_canary_policy_not_allowed")
        if request.quantity != 1 or (
            request.order_type == "limit" and request.limit_price is None
        ):
            raise ShioajiProductionError("live_canary_quantity_not_allowed")
        if manual and request.order_type != "limit":
            raise ShioajiProductionError("live_canary_policy_not_allowed")
        if request.order_type == "market" and request.purpose != "liquidation":
            raise ShioajiProductionError("guardian_market_requires_liquidation")
        broker_code = self.instrument_mapper.to_broker_contract(
            CanonicalInstrument(request.symbol, request.contract)
        )
        contract = self._contract(api, broker_code)
        try:
            order_factory = getattr(api, "Order", None)
            if not callable(order_factory):
                order_factory = getattr(getattr(self.sdk, "order", None), "Order", None)
            if not callable(order_factory):
                raise ShioajiProductionError("broker_order_factory_unavailable")
            is_market = request.order_type == "market"
            order = order_factory(
                action=self._constant("Action", "Buy" if request.side == "buy" else "Sell"),
                price=0 if is_market else request.limit_price,
                quantity=request.quantity,
                price_type=self._constant("FuturesPriceType", "MKT" if is_market else "LMT"),
                order_type=self._constant("OrderType", "IOC"),
                octype=self._constant("FuturesOCType", "Auto"),
                account=account,
            )
            self._external_order_calls += 1
            started = perf_counter()
            try:
                trade = await self._call(api.place_order, contract, order)
            finally:
                elapsed = (perf_counter() - started) * 1_000
                self._broker_response_total_ms += elapsed
                self._broker_response_max_ms = max(self._broker_response_max_ms, elapsed)
            report = self._trade_report(trade)
        except ShioajiProductionError:
            raise
        except Exception as exc:
            raise ShioajiProductionError("broker_submit_result_unknown") from exc
        if report.broker_order_id:
            self._trades[report.broker_order_id] = trade
        return report

    async def cancel(self, broker_order_id: str) -> ExternalOrderReport:
        if self.effective_mode != "canary":
            raise RuntimeError(READ_ONLY_ERROR)
        api, _account = self._require_ready()
        trade = self._trades.get(broker_order_id)
        if trade is None:
            try:
                trades, _ = await self._call(self._raw_snapshot)
                trade = next(
                    (item for item in trades if _order_id(item) == broker_order_id),
                    None,
                )
            except Exception as exc:
                raise ShioajiProductionError("broker_cancel_result_unknown") from exc
        if trade is None:
            raise ShioajiProductionError("broker_order_not_found")
        try:
            self._external_cancel_calls += 1
            started = perf_counter()
            try:
                returned = await self._call(api.cancel_order, trade)
            finally:
                elapsed = (perf_counter() - started) * 1_000
                self._broker_response_total_ms += elapsed
                self._broker_response_max_ms = max(self._broker_response_max_ms, elapsed)
            if returned is not None:
                trade = returned
            else:
                await self._call(api.update_status, self.account)
            return self._trade_report(trade)
        except ShioajiProductionError:
            raise
        except Exception as exc:
            raise ShioajiProductionError("broker_cancel_result_unknown") from exc

    async def replace(
        self, broker_order_id: str, request: BrokerOrderRequest
    ) -> ExternalOrderReport:
        raise RuntimeError(READ_ONLY_ERROR)

    def _constant(self, group: str, name: str) -> object:
        constants = getattr(self.sdk, "constant")
        return getattr(getattr(constants, group), name)

    @staticmethod
    def _contract(api: object, code: str) -> object:
        contracts = getattr(api, "Contracts", getattr(api, "contracts", None))
        futures = getattr(contracts, "Futures", contracts)
        getter = getattr(futures, "get", None)
        contract = getter(code) if callable(getter) else None
        if contract is None:
            try:
                contract = futures[code]
            except (KeyError, TypeError):
                contract = None
        if contract is None:
            raise ShioajiProductionError("broker_instrument_unavailable")
        return contract

    def _trade_report(self, trade: object) -> ExternalOrderReport:
        broker_order_id = _order_id(trade)
        if not broker_order_id:
            raise ShioajiProductionError("broker_submit_result_unknown")
        status = _status(trade)
        deals = _field(_field(trade, "status", {}), "deals", ()) or ()
        quantity = sum(
            int(_field(item, "quantity", _field(item, "qty", 0)) or 0)
            for item in deals
        )
        average = (
            sum(
                float(_field(item, "price", 0) or 0)
                * int(_field(item, "quantity", _field(item, "qty", 0)) or 0)
                for item in deals
            ) / quantity
            if quantity else None
        )
        return ExternalOrderReport(
            broker_order_id=broker_order_id,
            status=status.value,  # type: ignore[arg-type]
            occurred_at=self.now(),
            filled_quantity=quantity,
            average_fill_price=average,
            reason="shioaji_broker_response",
        )
