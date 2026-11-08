"""Writes domain orders straight into the tables for integration tests (no app writes yet)."""

from collections.abc import Sequence
from typing import Any

from sqlalchemy import insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from deliverius.db.tables import (
    CustomerRow,
    DeliveryEventRow,
    OrderItemRow,
    OrderRow,
    RestaurantRow,
)
from deliverius.orders.models import Order


def customer_values(order: Order) -> dict[str, Any]:
    # The tail of a UUID v7 is random; the head is a timestamp shared by IDs made in the same ms.
    tag = order.customer_id.hex[-12:]
    return {
        "id": order.customer_id,
        "sandbox_id": order.sandbox_id,
        "name": f"Cliente {tag}",
        "cpf": tag[:11],
        "phone": "+55 11 90000-0000",
        "email": f"{tag}@example.com",
        "address": "Rua Exemplo, 1",
    }


def order_values(order: Order) -> dict[str, Any]:
    return {
        "id": order.id,
        "sandbox_id": order.sandbox_id,
        "customer_id": order.customer_id,
        "restaurant_id": order.restaurant_id,
        "status": order.status.value,
        "payment_method": order.payment_method.value,
        "placed_at": order.placed_at,
        "estimated_delivery_at": order.estimated_delivery_at,
        "delivered_at": order.delivered_at,
        "subtotal_cents": order.subtotal.cents,
        "delivery_fee_cents": order.delivery_fee.cents,
        "discount_cents": order.discount.cents,
        "total_cents": order.total.cents,
        "notes": order.notes,
    }


async def insert_orders(session: AsyncSession, orders: Sequence[Order]) -> None:
    """Batch insert: one statement per table, whatever the number of orders."""
    restaurants = [
        {"id": rid, "name": "Cantina Teste", "category": "italiana"}
        for rid in {order.restaurant_id for order in orders}
    ]
    customers = list({order.customer_id: customer_values(order) for order in orders}.values())
    items = [
        {
            "id": item.id,
            "order_id": order.id,
            "name": item.name,
            "unit_price_cents": item.unit_price.cents,
            "quantity": item.quantity,
            "allergens": list(item.allergens),
        }
        for order in orders
        for item in order.items
    ]
    # Inserted newest first, so reading them in order proves the ORDER BY, not insertion luck.
    events = [
        {
            "id": event.id,
            "order_id": order.id,
            "status": event.status.value,
            "actor": event.actor.value,
            "occurred_at": event.occurred_at,
        }
        for order in orders
        for event in reversed(order.events)
    ]
    await session.execute(pg_insert(RestaurantRow).on_conflict_do_nothing(), restaurants)
    await session.execute(pg_insert(CustomerRow).on_conflict_do_nothing(), customers)
    await session.execute(insert(OrderRow), [order_values(order) for order in orders])
    if items:
        await session.execute(insert(OrderItemRow), items)
    if events:
        await session.execute(insert(DeliveryEventRow), events)
