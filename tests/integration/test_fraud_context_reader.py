"""The fraud history read from Postgres: what counts as a prior claim, scope, ceiling and plan."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import insert, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from support.action_rows import action_values, approval_values, decision_values
from support.order_builders import build_order
from support.order_rows import insert_orders
from support.sql_recorder import StatementRecorder

from deliverius.db.tables import ActionRow, ApprovalRequestRow, ConversationRow, CustomerRow
from deliverius.fraud.features import MAX_ROWS_PER_WINDOW
from deliverius.fraud.repository import PostgresFraudContextReader
from deliverius.orders import Order
from deliverius.policy import ActionKind
from deliverius.shared.access import CustomerScope
from deliverius.shared.errors import NotFoundError
from deliverius.shared.ids import CustomerId, SandboxId, new_id

pytestmark = pytest.mark.integration

CLAIMED_AT = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
CREATED_AT = datetime(2025, 3, 1, 9, 0, tzinfo=UTC)
NEIGHBOURS = 60
OLD_REQUESTS = 400


def days_before(days: float) -> datetime:
    return CLAIMED_AT - timedelta(days=days)


async def seed_customer(
    session: AsyncSession, placed_days_before: tuple[float, ...], scope: CustomerScope | None = None
) -> tuple[CustomerScope, list[Order], UUID]:
    """A customer with orders placed so many days before the claim, and one conversation."""
    scope = scope or CustomerScope(SandboxId(new_id()), CustomerId(new_id()))
    orders = [
        build_order(
            sandbox_id=scope.sandbox_id, customer_id=scope.customer_id, placed_at=days_before(d)
        )
        for d in placed_days_before
    ]
    await insert_orders(session, orders)
    await session.execute(
        update(CustomerRow).where(CustomerRow.id == scope.customer_id).values(created_at=CREATED_AT)
    )
    conversation_id = new_id()
    await session.execute(
        insert(ConversationRow).values(
            id=conversation_id,
            sandbox_id=scope.sandbox_id,
            customer_id=scope.customer_id,
            order_id=orders[0].id,
            status="open",
        )
    )
    return scope, orders, conversation_id


async def add_approval(
    session: AsyncSession, order: Order, conversation_id: UUID, claimed_at: datetime, **extra: Any
) -> dict[str, Any]:
    values = approval_values(
        order,
        conversation_id,
        claimed_at=claimed_at,
        proposal={"kind": "refund", "reason": "item_missing", "amount_cents": 4_200},
        **extra,
    )
    await session.execute(insert(ApprovalRequestRow).values(**values))
    return values


async def add_automatic_action(
    session: AsyncSession, order: Order, conversation_id: UUID, executed_at: datetime, **extra: Any
) -> None:
    values = action_values(
        order, conversation_id, claimed_at=executed_at, executed_at=executed_at, **extra
    )
    await session.execute(insert(ActionRow).values(**values))


async def test_a_prior_claim_is_an_approval_or_an_automatic_action_counted_once(
    session: AsyncSession,
) -> None:
    scope, orders, conversation = await seed_customer(session, (1, 10, 60, 100, 0, -1))
    order = orders[0]
    approved = await add_approval(
        session, order, conversation, days_before(20), **decision_values()
    )
    # The action executed from the approval: already counted by the approval.
    await session.execute(
        insert(ActionRow).values(
            **action_values(
                order,
                conversation,
                idempotency_key=approved["proposal_key"],
                approved_by="reviewer",
                approval_request_id=approved["id"],
                approval_status="approved",
                claimed_at=days_before(20),
                executed_at=days_before(19),
            )
        )
    )
    await add_approval(
        session, order, conversation, days_before(5), **decision_values("rejected", "Fraude")
    )
    await add_automatic_action(session, order, conversation, days_before(3), amount_cents=1_500)
    await add_automatic_action(session, order, conversation, CLAIMED_AT)
    await add_automatic_action(session, order, conversation, CLAIMED_AT + timedelta(hours=1))
    await add_approval(
        session, order, conversation, days_before(100), **decision_values("rejected", "Velho")
    )

    history = await PostgresFraudContextReader(session).history(scope, CLAIMED_AT)

    assert history.customer.created_at == CREATED_AT
    assert history.order_placed_at == (days_before(1), days_before(10), days_before(60))
    assert [(c.at, c.kind, c.requested.cents, c.reviewed) for c in history.claims] == [
        (days_before(3), ActionKind.REFUND, 1_500, False),
        (days_before(5), ActionKind.REFUND, 4_200, True),
        (days_before(20), ActionKind.REFUND, 4_200, True),
    ]


async def test_other_customers_and_other_sandboxes_never_count(session: AsyncSession) -> None:
    scope, _, _ = await seed_customer(session, (2,))
    neighbour = CustomerScope(scope.sandbox_id, CustomerId(new_id()))
    stranger = CustomerScope(SandboxId(new_id()), CustomerId(new_id()))
    for other in (neighbour, stranger):
        _, orders, conversation = await seed_customer(session, (1, 3), other)
        await add_automatic_action(session, orders[0], conversation, days_before(2))
        await add_approval(session, orders[0], conversation, days_before(4))

    history = await PostgresFraudContextReader(session).history(scope, CLAIMED_AT)

    assert history.order_placed_at == (days_before(2),)
    assert history.claims == ()


async def test_a_customer_outside_the_scope_is_not_found(session: AsyncSession) -> None:
    scope, _, _ = await seed_customer(session, (2,))
    other_sandbox = CustomerScope(SandboxId(new_id()), scope.customer_id)

    with pytest.raises(NotFoundError):
        await PostgresFraudContextReader(session).history(other_sandbox, CLAIMED_AT)


async def test_the_reader_stops_at_the_row_ceiling_newest_first(session: AsyncSession) -> None:
    rows = MAX_ROWS_PER_WINDOW + 5
    minutes = [1 + index for index in range(rows)]
    scope, orders, conversation = await seed_customer(session, tuple(m / 1_440 for m in minutes))
    await session.execute(
        insert(ActionRow),
        [
            action_values(
                orders[0],
                conversation,
                claimed_at=CLAIMED_AT - timedelta(minutes=m),
                executed_at=CLAIMED_AT - timedelta(minutes=m),
            )
            for m in minutes
        ],
    )

    history = await PostgresFraudContextReader(session).history(scope, CLAIMED_AT)

    assert len(history.order_placed_at) == MAX_ROWS_PER_WINDOW
    assert len(history.claims) == MAX_ROWS_PER_WINDOW
    assert history.claims[0].at == CLAIMED_AT - timedelta(minutes=1)
    assert history.order_placed_at[-1] > CLAIMED_AT - timedelta(minutes=rows)


async def test_the_three_queries_walk_their_indexes(session: AsyncSession) -> None:
    scope, orders, conversation = await seed_customer(session, (1, 5, 30))
    await add_approval(session, orders[0], conversation, days_before(4))
    await add_automatic_action(session, orders[0], conversation, days_before(2))
    # The case the new index exists for: a long history, almost all of it outside the window.
    # With the customer's index alone, every old request would be read and filtered.
    await session.execute(
        insert(ApprovalRequestRow),
        [
            approval_values(
                orders[0],
                conversation,
                claimed_at=days_before(91 + day),
                **decision_values("rejected", "Antigo"),
            )
            for day in range(OLD_REQUESTS)
        ],
    )
    # A sandbox holds many customers with their own orders and claims: the plan must find this
    # customer's rows by the customer, not walk the whole sandbox.
    for _ in range(NEIGHBOURS):
        neighbour = CustomerScope(scope.sandbox_id, CustomerId(new_id()))
        _, theirs, talk = await seed_customer(session, (1, 4, 9, 20, 40, 70), neighbour)
        await add_approval(session, theirs[0], talk, days_before(6))
        await add_automatic_action(session, theirs[0], talk, days_before(3))
    for table in ("customers", "orders", "approval_requests", "actions"):
        await session.execute(text(f"ANALYZE {table}"))

    with StatementRecorder(session) as recorder:
        await PostgresFraudContextReader(session).history(scope, CLAIMED_AT)

    # Small test tables make a sequential scan look cheapest; turning it off asks whether a plan
    # that walks the indexes exists at all.
    await session.execute(text("SET LOCAL enable_seqscan = off"))
    connection = await session.connection()
    plans = []
    for statement, parameters in recorder.statements:
        result = await connection.exec_driver_sql(f"EXPLAIN {statement}", parameters)
        plans.append("\n".join(row[0] for row in result))
    customer_plan, orders_plan, claims_plan = plans

    # Either index that starts with the customer id finds one row.
    assert "pk_customers" in customer_plan or "uq_customers_id_sandbox_id" in customer_plan, (
        customer_plan
    )
    assert "ix_orders_customer_listing" in orders_plan, orders_plan
    assert "ix_approval_requests_sandbox_id_customer_id_claimed_at" in claims_plan, claims_plan
    assert "ix_actions_sandbox_id_customer_id_executed_at" in claims_plan, claims_plan
    for plan in plans:
        assert "Seq Scan" not in plan, plan
