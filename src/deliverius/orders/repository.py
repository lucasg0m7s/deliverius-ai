from datetime import datetime
from typing import assert_never
from uuid import UUID

from sqlalchemy import ColumnElement, Row, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from deliverius.db.errors import translate_db_errors
from deliverius.db.tables import CustomerRow, DeliveryEventRow, OrderItemRow, OrderRow
from deliverius.orders.cursor import Page, PageRequest, decode_cursor, page_from_rows
from deliverius.orders.models import (
    DeliveryEvent,
    EventActor,
    Order,
    OrderItem,
    OrderStatus,
    OrderSummary,
    PaymentMethod,
    can_transition,
)
from deliverius.shared.access import AccessScope, CustomerScope, SandboxScope
from deliverius.shared.clock import require_aware
from deliverius.shared.errors import InvariantError, NotFoundError
from deliverius.shared.ids import (
    CustomerId,
    DeliveryEventId,
    OrderId,
    OrderItemId,
    RestaurantId,
    SandboxId,
    new_id,
)
from deliverius.shared.money import Money

_CANCELLABLE = tuple(
    status.value for status in OrderStatus if can_transition(status, OrderStatus.CANCELLED)
)


class PostgresOrderRepository:
    """`OrderReader` and `OrderLocker` on Postgres. Uses the caller's session and never commits."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_order(self, scope: AccessScope, order_id: OrderId) -> Order | None:
        # One query for the order (scope in the WHERE) plus one per collection, at any item count.
        statement = (
            select(OrderRow)
            .where(OrderRow.id == order_id, *_scope_filter(scope))
            .options(selectinload(OrderRow.items), selectinload(OrderRow.events))
        )
        with translate_db_errors():
            row = (await self._session.execute(statement)).scalar_one_or_none()
        return None if row is None else _to_order(row)

    async def list_orders(self, scope: AccessScope, page: PageRequest) -> Page[OrderSummary]:
        after = decode_cursor(page.cursor) if page.cursor is not None else None
        statement = (
            select(
                OrderRow.id,
                OrderRow.customer_id,
                OrderRow.restaurant_id,
                OrderRow.status,
                OrderRow.placed_at,
                OrderRow.total_cents,
            )
            .where(*_scope_filter(scope))
            .order_by(OrderRow.placed_at.desc(), OrderRow.id.desc())
            .limit(page.limit + 1)
        )
        if after is not None:
            statement = statement.where(
                tuple_(OrderRow.placed_at, OrderRow.id) < tuple_(after.placed_at, after.id)
            )
        with translate_db_errors():
            rows = (await self._session.execute(statement)).all()
        return page_from_rows([_to_summary(row) for row in rows], page.limit)

    async def lock_customer(self, scope: CustomerScope) -> CustomerId | None:
        # Only the ID is read: no personal data leaves the table just to be locked.
        statement = (
            select(CustomerRow.id)
            .where(CustomerRow.id == scope.customer_id, CustomerRow.sandbox_id == scope.sandbox_id)
            # NO KEY UPDATE still serializes lockers, but does not block inserts that reference
            # the customer (a new order, a message) the way a plain FOR UPDATE would.
            .with_for_update(key_share=True)
        )
        with translate_db_errors():
            locked = (await self._session.execute(statement)).scalar_one_or_none()
        return None if locked is None else CustomerId(locked)

    async def lock_order(self, scope: CustomerScope, order_id: OrderId) -> Order | None:
        # `populate_existing`: an order this session read before the lock must be read again.
        statement = (
            select(OrderRow)
            .where(OrderRow.id == order_id, *_scope_filter(scope))
            .options(selectinload(OrderRow.items), selectinload(OrderRow.events))
            .with_for_update(of=OrderRow)
            .execution_options(populate_existing=True)
        )
        with translate_db_errors():
            row = (await self._session.execute(statement)).scalar_one_or_none()
        return None if row is None else _to_order(row)

    async def mark_cancelled(self, scope: CustomerScope, order_id: OrderId, at: datetime) -> None:
        require_aware(at, "at")
        # One conditional statement: the status check and the change cannot be split by a writer.
        statement = (
            update(OrderRow)
            .where(
                OrderRow.id == order_id,
                *_scope_filter(scope),
                OrderRow.status.in_(_CANCELLABLE),
            )
            .values(status=OrderStatus.CANCELLED.value)
            .returning(OrderRow.id)
        )
        with translate_db_errors():
            changed = (await self._session.execute(statement)).scalar_one_or_none()
            if changed is None:
                await self._raise_why_not_cancellable(scope, order_id)
            self._session.add(
                DeliveryEventRow(
                    id=new_id(),
                    order_id=order_id,
                    status=OrderStatus.CANCELLED.value,
                    actor=EventActor.SYSTEM.value,
                    occurred_at=at,
                )
            )
            await self._session.flush()

    async def _raise_why_not_cancellable(self, scope: CustomerScope, order_id: OrderId) -> None:
        statement = select(OrderRow.status).where(OrderRow.id == order_id, *_scope_filter(scope))
        status = (await self._session.execute(statement)).scalar_one_or_none()
        if status is None:
            raise NotFoundError
        raise InvariantError("order.cancel_transition_not_allowed")


def _scope_filter(scope: AccessScope) -> list[ColumnElement[bool]]:
    match scope:
        case CustomerScope(sandbox_id=sandbox_id, customer_id=customer_id):
            return [OrderRow.sandbox_id == sandbox_id, OrderRow.customer_id == customer_id]
        case SandboxScope(sandbox_id=sandbox_id):
            return [OrderRow.sandbox_id == sandbox_id]
        case _:
            assert_never(scope)


def _to_summary(row: Row[UUID, UUID, UUID, str, datetime, int]) -> OrderSummary:
    return OrderSummary(
        id=OrderId(row.id),
        customer_id=CustomerId(row.customer_id),
        restaurant_id=RestaurantId(row.restaurant_id),
        status=OrderStatus(row.status),
        placed_at=row.placed_at,
        total=Money(row.total_cents),
    )


def _to_order(row: OrderRow) -> Order:
    return Order(
        id=OrderId(row.id),
        sandbox_id=SandboxId(row.sandbox_id),
        customer_id=CustomerId(row.customer_id),
        restaurant_id=RestaurantId(row.restaurant_id),
        status=OrderStatus(row.status),
        payment_method=PaymentMethod(row.payment_method),
        placed_at=row.placed_at,
        estimated_delivery_at=row.estimated_delivery_at,
        delivered_at=row.delivered_at,
        subtotal=Money(row.subtotal_cents),
        delivery_fee=Money(row.delivery_fee_cents),
        discount=Money(row.discount_cents),
        total=Money(row.total_cents),
        notes=row.notes,
        items=tuple(_to_item(item) for item in row.items),
        events=tuple(_to_event(event) for event in row.events),
    )


def _to_item(row: OrderItemRow) -> OrderItem:
    return OrderItem(
        id=OrderItemId(row.id),
        name=row.name,
        unit_price=Money(row.unit_price_cents),
        quantity=row.quantity,
        allergens=tuple(row.allergens),
    )


def _to_event(row: DeliveryEventRow) -> DeliveryEvent:
    return DeliveryEvent(
        id=DeliveryEventId(row.id),
        status=OrderStatus(row.status),
        actor=EventActor(row.actor),
        occurred_at=row.occurred_at,
    )
