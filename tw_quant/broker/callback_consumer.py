from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Callable

from .audit import (
    BrokerEventAuditRecord,
    BrokerEventAuditStatus,
    BrokerEventAuditStore,
)
from .events import BrokerEvent
from .manager import LiveOrderManager


class BrokerCallbackConsumer:
    """Persist callback evidence, then reconcile against broker truth."""

    def __init__(
        self,
        audit_store: BrokerEventAuditStore,
        order_manager: LiveOrderManager,
        *,
        now: Callable[[], datetime] | None = None,
    ):
        self.audit_store = audit_store
        self.order_manager = order_manager
        self.now = now or (lambda: datetime.now(timezone.utc))

    async def consume_one(self, event: BrokerEvent) -> BrokerEventAuditRecord:
        record, _created = self.audit_store.record_received(event)
        if record.status is BrokerEventAuditStatus.RECONCILED:
            return record
        if not event.account_id:
            return self.audit_store.mark(
                event.event_id,
                BrokerEventAuditStatus.UNMATCHED,
                updated_at=self.now(),
                error="callback_has_no_account_id",
            )
        if not event.broker_order_id:
            return self.audit_store.mark(
                event.event_id,
                BrokerEventAuditStatus.UNMATCHED,
                updated_at=self.now(),
                error="callback_has_no_broker_order_id",
            )
        try:
            order = await self.order_manager.reconcile_by_broker_order_id(
                event.account_ref, event.broker_order_id
            )
        except Exception as exc:
            code = getattr(exc, "code", None)
            return self.audit_store.mark(
                event.event_id,
                BrokerEventAuditStatus.FAILED,
                updated_at=self.now(),
                error=(
                    str(code)
                    if isinstance(code, str) and code
                    else "broker_refresh_failed"
                ),
            )
        if order is None:
            return self.audit_store.mark(
                event.event_id,
                BrokerEventAuditStatus.UNMATCHED,
                updated_at=self.now(),
                error="local_order_not_found",
            )
        return self.audit_store.mark(
            event.event_id,
            BrokerEventAuditStatus.RECONCILED,
            updated_at=self.now(),
            order=order,
        )

    async def run(
        self,
        queue: asyncio.Queue[BrokerEvent],
        stop_event: asyncio.Event,
    ) -> None:
        """Consume until stopped; queue items are acknowledged after audit handling."""

        while not stop_event.is_set():
            try:
                event = await asyncio.wait_for(queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            try:
                await self.consume_one(event)
            finally:
                queue.task_done()
