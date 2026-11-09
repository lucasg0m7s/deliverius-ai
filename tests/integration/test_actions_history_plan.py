"""The history query walks the two action indexes, never the whole table."""

from datetime import timedelta

import pytest
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import AsyncSession
from support.action_builders import CLAIMED_AT, delivered_order, delivered_order_of, scope_of
from support.action_rows import action_values, conversation_values
from support.order_builders import build_item
from support.order_rows import insert_orders
from support.sql_recorder import StatementRecorder

from deliverius.actions import PostgresActionStore
from deliverius.db.tables import ActionRow, ConversationRow
from deliverius.orders import Order
from deliverius.policy import POLICY_LIMITS

pytestmark = pytest.mark.integration

CUSTOMERS = 20
ORDERS_PER_CUSTOMER = 6


async def seed_actions(session: AsyncSession) -> list[Order]:
    """Many customers with several orders each and one action per order, over 90 days."""
    orders: list[Order] = []
    for _ in range(CUSTOMERS):
        first = delivered_order(build_item("Combo", 3_000))
        orders.append(first)
        orders += [delivered_order_of(first, 3_000) for _ in range(ORDERS_PER_CUSTOMER - 1)]
    await insert_orders(session, orders)
    conversations = [conversation_values(order) for order in orders]
    await session.execute(insert(ConversationRow), conversations)
    await session.execute(
        insert(ActionRow),
        [
            action_values(
                order,
                conversation["id"],
                executed_at=CLAIMED_AT - timedelta(days=index % 90),
            )
            for index, (order, conversation) in enumerate(zip(orders, conversations, strict=True))
        ],
    )
    await session.execute(text("ANALYZE actions"))
    return orders


async def test_history_uses_both_action_indexes_and_no_sequential_scan(
    session: AsyncSession,
) -> None:
    orders = await seed_actions(session)
    order = orders[0]
    since = CLAIMED_AT - POLICY_LIMITS.customer_auto_approval_window
    store = PostgresActionStore(session)

    with StatementRecorder(session) as recorder:
        history = await store.history(scope_of(order), order.id, since)

    assert history.actions
    [(statement, parameters)] = recorder.statements
    # Small test tables make a sequential scan look cheapest; turning it off asks whether a plan
    # that walks the indexes exists at all.
    await session.execute(text("SET LOCAL enable_seqscan = off"))
    connection = await session.connection()
    result = await connection.exec_driver_sql(f"EXPLAIN {statement}", parameters)
    plan = "\n".join(row[0] for row in result)

    assert "ix_actions_sandbox_id_customer_id_executed_at" in plan, plan
    assert "ix_actions_order_id_executed_at" in plan, plan
    assert "Seq Scan" not in plan, plan


async def test_history_keeps_the_customer_window_and_the_whole_order(
    session: AsyncSession,
) -> None:
    orders = await seed_actions(session)
    # Customer 6 owns orders 36 to 41, whose actions are 36 to 41 days old: all outside the window.
    order = orders[36]
    since = CLAIMED_AT - POLICY_LIMITS.customer_auto_approval_window

    history = await PostgresActionStore(session).history(scope_of(order), order.id, since)

    assert [prior.order_id for prior in history.actions] == [order.id]
