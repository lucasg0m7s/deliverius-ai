from dataclasses import replace
from datetime import datetime
from typing import assert_never

from deliverius.orders.cursor import Page, PageRequest, decode_cursor, page_from_rows
from deliverius.orders.models import (
    DeliveryEvent,
    EventActor,
    Order,
    OrderStatus,
    OrderSummary,
    can_transition,
)
from deliverius.shared.access import AccessScope, CustomerScope, SandboxScope
from deliverius.shared.clock import require_aware
from deliverius.shared.errors import InvariantError, NotFoundError
from deliverius.shared.ids import CustomerId, DeliveryEventId, OrderId, new_id


class InMemoryOrderRepository:
    """`OrderReader` and `OrderLocker` kept in a dict, with the same contract as Postgres.

    For tests of code that reads or changes orders; the shared contract suites keep both
    implementations interchangeable. Locks are no-ops: unit tests run one operation at a time, and
    waiting for a lock is proved against Postgres.
    """

    def __init__(self, orders: list[Order] | None = None) -> None:
        self._orders: dict[OrderId, Order] = {}
        for order in orders or []:
            self.add(order)

    def add(self, order: Order) -> None:
        self._orders[order.id] = _with_timeline_in_order(order)

    async def get_order(self, scope: AccessScope, order_id: OrderId) -> Order | None:
        order = self._orders.get(order_id)
        return order if order is not None and _in_scope(scope, order) else None

    async def list_orders(self, scope: AccessScope, page: PageRequest) -> Page[OrderSummary]:
        after = decode_cursor(page.cursor) if page.cursor is not None else None
        visible = sorted(
            (order.summary() for order in self._orders.values() if _in_scope(scope, order)),
            key=lambda summary: (summary.placed_at, summary.id),
            reverse=True,
        )
        if after is not None:
            visible = [s for s in visible if (s.placed_at, s.id) < (after.placed_at, after.id)]
        return page_from_rows(visible[: page.limit + 1], page.limit)

    async def lock_customer(self, scope: CustomerScope) -> CustomerId | None:
        known = any(_in_scope(scope, order) for order in self._orders.values())
        return scope.customer_id if known else None

    async def lock_order(self, scope: CustomerScope, order_id: OrderId) -> Order | None:
        return await self.get_order(scope, order_id)

    async def mark_cancelled(self, scope: CustomerScope, order_id: OrderId, at: datetime) -> None:
        require_aware(at, "at")
        order = await self.get_order(scope, order_id)
        if order is None:
            raise NotFoundError
        if not can_transition(order.status, OrderStatus.CANCELLED):
            raise InvariantError("order.cancel_transition_not_allowed")
        cancelled = DeliveryEvent(
            id=DeliveryEventId(new_id()),
            status=OrderStatus.CANCELLED,
            actor=EventActor.SYSTEM,
            occurred_at=at,
        )
        self.add(replace(order, status=OrderStatus.CANCELLED, events=(*order.events, cancelled)))


def _in_scope(scope: AccessScope, order: Order) -> bool:
    match scope:
        case CustomerScope(sandbox_id=sandbox_id, customer_id=customer_id):
            return order.sandbox_id == sandbox_id and order.customer_id == customer_id
        case SandboxScope(sandbox_id=sandbox_id):
            return order.sandbox_id == sandbox_id
        case _:
            assert_never(scope)


def _with_timeline_in_order(order: Order) -> Order:
    """Same ordering the Postgres repository applies: items by ID, events by instant then ID."""
    return replace(
        order,
        items=tuple(sorted(order.items, key=lambda item: item.id)),
        events=tuple(sorted(order.events, key=lambda event: (event.occurred_at, event.id))),
    )
