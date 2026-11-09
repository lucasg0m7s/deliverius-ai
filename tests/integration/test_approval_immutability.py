"""A decided approval cannot be rewritten, so what the executor reads stays true until it acts."""

from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from support.action_rows import decision_values, insert_approval, insert_order_and_conversation

from deliverius.db.tables import ApprovalRequestRow
from deliverius.orders.models import Order
from deliverius.shared.ids import new_id

pytestmark = pytest.mark.integration


async def stored(session: AsyncSession, approval_id: UUID) -> ApprovalRequestRow:
    statement = select(ApprovalRequestRow).where(ApprovalRequestRow.id == approval_id)
    return (await session.execute(statement.execution_options(populate_existing=True))).scalar_one()


async def decided_request(session: AsyncSession, status: str) -> tuple[Order, dict[str, Any]]:
    order, conversation_id = await insert_order_and_conversation(session)
    values = await insert_approval(session, order, conversation_id, **decision_values(status))
    return order, values


@pytest.mark.parametrize("status", ["approved", "rejected"])
@pytest.mark.parametrize(
    "changes",
    [
        {"status": "pending", "decided_by": None, "decided_at": None, "decision_reason": None},
        {"decision_reason": "Rewritten"},
        {"status": "rejected", "decision_reason": "Changed my mind"},
        {"version": 99},
        {"policy_reasons": ["marked_as_delivered"]},
    ],
    ids=["back-to-pending", "reason", "flipped-decision", "version", "policy-reasons"],
)
async def test_update_of_a_decided_request_is_refused_and_the_row_stays(
    session: AsyncSession, status: str, changes: dict[str, Any]
) -> None:
    _, values = await decided_request(session, status)
    statement = update(ApprovalRequestRow).where(ApprovalRequestRow.id == values["id"])

    with pytest.raises(DBAPIError, match="immutable"):
        async with session.begin_nested():
            await session.execute(statement.values(**changes))

    row = await stored(session, values["id"])
    assert (row.status, row.decision_reason, row.version) == (status, "Looks right", 2)


@pytest.mark.parametrize("status", ["approved", "rejected"])
async def test_delete_of_a_decided_request_is_refused_and_the_row_stays(
    session: AsyncSession, status: str
) -> None:
    _, values = await decided_request(session, status)

    with pytest.raises(DBAPIError, match="immutable"):
        async with session.begin_nested():
            await session.execute(
                delete(ApprovalRequestRow).where(ApprovalRequestRow.id == values["id"])
            )

    assert (await stored(session, values["id"])).status == status


async def test_pending_request_can_receive_the_fraud_assessment(session: AsyncSession) -> None:
    order, conversation_id = await insert_order_and_conversation(session)
    values = await insert_approval(session, order, conversation_id)

    await session.execute(
        update(ApprovalRequestRow)
        .where(ApprovalRequestRow.id == values["id"])
        .values(fraud_score=0.25, fraud_model_version="fraud-1")
    )

    row = await stored(session, values["id"])
    assert (row.fraud_score, row.fraud_model_version, row.status) == (0.25, "fraud-1", "pending")


@pytest.mark.parametrize("status", ["approved", "rejected"])
async def test_pending_request_can_be_decided_once(session: AsyncSession, status: str) -> None:
    order, conversation_id = await insert_order_and_conversation(session)
    values = await insert_approval(session, order, conversation_id)
    decision = decision_values(status, "Checked the order")

    await session.execute(
        update(ApprovalRequestRow).where(ApprovalRequestRow.id == values["id"]).values(**decision)
    )

    row = await stored(session, values["id"])
    assert (row.status, row.version, row.decision_reason) == (status, 2, "Checked the order")
    with pytest.raises(DBAPIError, match="immutable"):
        async with session.begin_nested():
            await session.execute(
                update(ApprovalRequestRow)
                .where(ApprovalRequestRow.id == values["id"])
                .values(decided_by=new_id())
            )


async def test_pending_request_can_be_deleted(session: AsyncSession) -> None:
    order, conversation_id = await insert_order_and_conversation(session)
    values = await insert_approval(session, order, conversation_id)

    await session.execute(delete(ApprovalRequestRow).where(ApprovalRequestRow.id == values["id"]))

    remaining = await session.scalar(
        select(ApprovalRequestRow.id).where(ApprovalRequestRow.id == values["id"])
    )
    assert remaining is None
