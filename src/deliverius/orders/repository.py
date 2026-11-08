from datetime import datetime
from typing import assert_never
from uuid import UUID

from sqlalchemy import ColumnElement, Row, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from deliverius.db.errors import translate_db_errors
from deliverius.db.tables import DeliveryEventRow, OrderItemRow, OrderRow
from deliverius.orders.cursor import Page, PageRequest, decode_cursor, page_from_rows
from deliverius.orders.models import (
    DeliveryEvent,
    EventActor,
    Order,
    OrderItem,
    OrderStatus,
    OrderSummary,
    PaymentMethod,
)
from deliverius.shared.access import AccessScope, CustomerScope, SandboxScope
from deliverius.shared.ids import (
    CustomerId,
    DeliveryEventId,
    OrderId,
    OrderItemId,
    RestaurantId,
    SandboxId,
)
from deliverius.shared.money import Money


class PostgresOrderRepository:
    """`OrderReader` on Postgres. Uses the caller's session and never commits."""

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
