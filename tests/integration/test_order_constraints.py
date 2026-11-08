"""The database is the last barrier: it refuses inconsistent orders even if the code is wrong."""

from typing import Any

import pytest
from sqlalchemy import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from support.order_builders import build_order
from support.order_rows import insert_orders, order_values

from deliverius.db.tables import OrderItemRow, OrderRow
from deliverius.orders.models import Order
from deliverius.shared.ids import CustomerId, SandboxId, new_id

pytestmark = pytest.mark.integration


async def stored_order(session: AsyncSession) -> Order:
    order = build_order(sandbox_id=SandboxId(new_id()), customer_id=CustomerId(new_id()))
    await insert_orders(session, [order])
    return order


async def assert_refused(session: AsyncSession, table: Any, values: dict[str, Any]) -> None:
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await session.execute(insert(table).values(**values))


@pytest.mark.parametrize(
    "overrides",
    [
        {"total_cents": 1},
        {"subtotal_cents": -100, "total_cents": -100 + 799 - 500},
        {"discount_cents": 999_999, "total_cents": 0},
        {"status": "lost_in_space"},
        {"payment_method": "cash"},
    ],
    ids=["total-inconsistent", "negative", "discount-above-amount", "unknown-status", "cash"],
)
async def test_inconsistent_order_is_refused(
    session: AsyncSession, overrides: dict[str, Any]
) -> None:
    template = await stored_order(session)

    await assert_refused(session, OrderRow, order_values(template) | {"id": new_id()} | overrides)


async def test_order_pointing_at_a_customer_of_another_sandbox_is_refused(
    session: AsyncSession,
) -> None:
    template = await stored_order(session)
    other_sandbox = SandboxId(new_id())

    await assert_refused(
        session, OrderRow, order_values(template) | {"id": new_id(), "sandbox_id": other_sandbox}
    )


@pytest.mark.parametrize(
    "overrides", [{"quantity": 0}, {"unit_price_cents": 0}], ids=["quantity-zero", "free-item"]
)
async def test_item_without_positive_price_and_quantity_is_refused(
    session: AsyncSession, overrides: dict[str, Any]
) -> None:
    order = await stored_order(session)
    item = {
        "id": new_id(),
        "order_id": order.id,
        "name": "x",
        "unit_price_cents": 100,
        "quantity": 1,
    }

    await assert_refused(session, OrderItemRow, item | overrides)
