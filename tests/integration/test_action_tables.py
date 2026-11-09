"""The database is the last barrier for actions, approvals and handoffs, even if the code errs."""

from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from support.action_rows import (
    decision_values,
    insert_action,
    insert_approval,
    insert_conversation,
    insert_handoff,
    insert_order_and_conversation,
    new_order,
)
from support.order_rows import insert_orders

from deliverius.db.tables import HandoffRow
from deliverius.shared.ids import new_id

pytestmark = pytest.mark.integration


async def assert_refused(
    session: AsyncSession, constraint: str, attempt: Callable[[], Awaitable[object]]
) -> None:
    """The statement fails on `constraint` (matched by name) and the session stays usable."""
    with pytest.raises(IntegrityError, match=constraint):
        async with session.begin_nested():
            await attempt()


async def test_automatic_action_without_approval_is_accepted(session: AsyncSession) -> None:
    order, conversation_id = await insert_order_and_conversation(session)

    await insert_action(session, order, conversation_id)


async def test_reviewer_action_with_an_approved_request_of_the_same_proposal_is_accepted(
    session: AsyncSession,
) -> None:
    order, conversation_id = await insert_order_and_conversation(session)
    approval = await insert_approval(session, order, conversation_id, **decision_values())

    await insert_action(
        session,
        order,
        conversation_id,
        approved_by="reviewer",
        approval_request_id=approval["id"],
        approval_status="approved",
        idempotency_key=approval["proposal_key"],
    )


async def test_action_with_amount_zero_is_refused(session: AsyncSession) -> None:
    order, conversation_id = await insert_order_and_conversation(session)

    await assert_refused(
        session,
        "ck_actions_amount_positive",
        lambda: insert_action(session, order, conversation_id, amount_cents=0),
    )


@pytest.mark.parametrize(
    ("column", "value", "constraint"),
    [
        ("kind", "gift", "ck_actions_kind_known"),
        ("reason", "bored", "ck_actions_reason_known"),
        ("approved_by", "robot", "ck_actions_approved_by_known"),
        ("items", {"order_item_id": "x"}, "ck_actions_items_is_array"),
    ],
    ids=["kind", "reason", "approved-by", "items-not-an-array"],
)
async def test_action_with_an_unknown_value_is_refused(
    session: AsyncSession, column: str, value: object, constraint: str
) -> None:
    order, conversation_id = await insert_order_and_conversation(session)

    await assert_refused(
        session,
        constraint,
        lambda: insert_action(session, order, conversation_id, **{column: value}),
    )


async def test_repeated_idempotency_key_is_refused(session: AsyncSession) -> None:
    order, conversation_id = await insert_order_and_conversation(session)
    first = await insert_action(session, order, conversation_id)

    await assert_refused(
        session,
        "uq_actions_idempotency_key",
        lambda: insert_action(
            session, order, conversation_id, idempotency_key=first["idempotency_key"]
        ),
    )


async def test_reviewer_action_without_an_approval_is_refused(session: AsyncSession) -> None:
    order, conversation_id = await insert_order_and_conversation(session)

    await assert_refused(
        session,
        "ck_actions_reviewer_has_approval",
        lambda: insert_action(session, order, conversation_id, approved_by="reviewer"),
    )


async def test_automatic_action_with_an_approval_is_refused(session: AsyncSession) -> None:
    order, conversation_id = await insert_order_and_conversation(session)
    approval = await insert_approval(session, order, conversation_id, **decision_values())

    await assert_refused(
        session,
        "ck_actions_reviewer_has_approval",
        lambda: insert_action(
            session,
            order,
            conversation_id,
            approval_request_id=approval["id"],
            approval_status="approved",
            idempotency_key=approval["proposal_key"],
        ),
    )


async def test_action_naming_an_approval_without_its_status_is_refused(
    session: AsyncSession,
) -> None:
    order, conversation_id = await insert_order_and_conversation(session)
    approval = await insert_approval(session, order, conversation_id, **decision_values())

    await assert_refused(
        session,
        "ck_actions_approval_columns_together",
        lambda: insert_action(
            session,
            order,
            conversation_id,
            approved_by="reviewer",
            approval_request_id=approval["id"],
            idempotency_key=approval["proposal_key"],
        ),
    )


async def test_action_claiming_a_status_other_than_approved_is_refused(
    session: AsyncSession,
) -> None:
    order, conversation_id = await insert_order_and_conversation(session)
    approval = await insert_approval(session, order, conversation_id)

    await assert_refused(
        session,
        "ck_actions_approval_is_approved",
        lambda: insert_action(
            session,
            order,
            conversation_id,
            approved_by="reviewer",
            approval_request_id=approval["id"],
            approval_status="pending",
            idempotency_key=approval["proposal_key"],
        ),
    )


async def test_action_pointing_at_a_pending_approval_is_refused_by_the_foreign_key(
    session: AsyncSession,
) -> None:
    order, conversation_id = await insert_order_and_conversation(session)
    pending = await insert_approval(session, order, conversation_id)

    await assert_refused(
        session,
        "fk_actions_approval_matches_request",
        lambda: insert_action(
            session,
            order,
            conversation_id,
            approved_by="reviewer",
            approval_request_id=pending["id"],
            approval_status="approved",
            idempotency_key=pending["proposal_key"],
        ),
    )


async def test_action_pointing_at_a_rejected_approval_is_refused_by_the_foreign_key(
    session: AsyncSession,
) -> None:
    order, conversation_id = await insert_order_and_conversation(session)
    rejected = await insert_approval(session, order, conversation_id, **decision_values("rejected"))

    await assert_refused(
        session,
        "fk_actions_approval_matches_request",
        lambda: insert_action(
            session,
            order,
            conversation_id,
            approved_by="reviewer",
            approval_request_id=rejected["id"],
            approval_status="approved",
            idempotency_key=rejected["proposal_key"],
        ),
    )


async def test_action_pointing_at_an_approval_of_another_conversation_is_refused(
    session: AsyncSession,
) -> None:
    order, conversation_id = await insert_order_and_conversation(session)
    other_conversation = await insert_conversation(session, order)
    approval = await insert_approval(session, order, other_conversation, **decision_values())

    await assert_refused(
        session,
        "fk_actions_approval_matches_request",
        lambda: insert_action(
            session,
            order,
            conversation_id,
            approved_by="reviewer",
            approval_request_id=approval["id"],
            approval_status="approved",
            idempotency_key=approval["proposal_key"],
        ),
    )


async def test_action_pointing_at_an_approval_of_another_proposal_is_refused(
    session: AsyncSession,
) -> None:
    order, conversation_id = await insert_order_and_conversation(session)
    approval = await insert_approval(session, order, conversation_id, **decision_values())

    await assert_refused(
        session,
        "fk_actions_approval_matches_request",
        lambda: insert_action(
            session,
            order,
            conversation_id,
            approved_by="reviewer",
            approval_request_id=approval["id"],
            approval_status="approved",
            idempotency_key=new_id().hex,
        ),
    )


async def test_action_on_an_order_other_than_the_conversations_is_refused(
    session: AsyncSession,
) -> None:
    order, conversation_id = await insert_order_and_conversation(session)
    other_order = new_order()
    await insert_orders(session, [other_order])

    await assert_refused(
        session,
        "fk_actions_conversation_matches_customer_order",
        lambda: insert_action(
            session,
            other_order,
            conversation_id,
            customer_id=order.customer_id,
            sandbox_id=order.sandbox_id,
        ),
    )


async def test_action_of_a_customer_other_than_the_conversations_is_refused(
    session: AsyncSession,
) -> None:
    _, conversation_id = await insert_order_and_conversation(session)
    stranger_order = new_order()
    await insert_orders(session, [stranger_order])

    await assert_refused(
        session,
        "fk_actions_conversation_matches_customer_order",
        lambda: insert_action(session, stranger_order, conversation_id),
    )


async def test_action_on_a_conversation_without_an_order_is_refused(session: AsyncSession) -> None:
    order = new_order()
    await insert_orders(session, [order])
    conversation_id = await insert_conversation(session, order, order_id=None)

    await assert_refused(
        session,
        "fk_actions_conversation_matches_customer_order",
        lambda: insert_action(session, order, conversation_id),
    )


async def test_second_pending_approval_in_the_same_conversation_is_refused(
    session: AsyncSession,
) -> None:
    order, conversation_id = await insert_order_and_conversation(session)
    await insert_approval(session, order, conversation_id)

    await assert_refused(
        session,
        "uq_approval_requests_pending_conversation",
        lambda: insert_approval(session, order, conversation_id),
    )


async def test_new_pending_approval_is_accepted_once_the_previous_one_is_decided(
    session: AsyncSession,
) -> None:
    order, conversation_id = await insert_order_and_conversation(session)
    await insert_approval(session, order, conversation_id, **decision_values())

    await insert_approval(session, order, conversation_id)


async def test_same_proposal_twice_in_a_conversation_is_refused(session: AsyncSession) -> None:
    order, conversation_id = await insert_order_and_conversation(session)
    first = await insert_approval(session, order, conversation_id, **decision_values())

    await assert_refused(
        session,
        "uq_approval_requests_conversation_id_proposal_key",
        lambda: insert_approval(
            session, order, conversation_id, proposal_key=first["proposal_key"]
        ),
    )


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"status": "approved"}, "ck_approval_requests_decision_complete"),
        (decision_values("pending"), "ck_approval_requests_decision_complete"),
        (decision_values(reason="x" * 501), "ck_approval_requests_decision_reason_length"),
        (decision_values(reason=""), "ck_approval_requests_decision_reason_length"),
        (decision_values("expired"), "ck_approval_requests_status_known"),
        ({"fraud_score": 1.5}, "ck_approval_requests_fraud_score_in_range"),
        ({"fraud_score": -0.1}, "ck_approval_requests_fraud_score_in_range"),
        ({"version": 0}, "ck_approval_requests_version_positive"),
    ],
    ids=[
        "decided-without-decision",
        "pending-with-decision",
        "reason-too-long",
        "reason-empty",
        "unknown-status",
        "fraud-score-above-one",
        "fraud-score-below-zero",
        "version-zero",
    ],
)
async def test_inconsistent_approval_request_is_refused(
    session: AsyncSession, overrides: dict[str, Any], constraint: str
) -> None:
    order, conversation_id = await insert_order_and_conversation(session)

    await assert_refused(
        session, constraint, lambda: insert_approval(session, order, conversation_id, **overrides)
    )


async def test_approval_with_a_reason_of_exactly_500_characters_is_accepted(
    session: AsyncSession,
) -> None:
    order, conversation_id = await insert_order_and_conversation(session)

    await insert_approval(session, order, conversation_id, **decision_values(reason="x" * 500))


async def test_approval_of_an_order_other_than_the_conversations_is_refused(
    session: AsyncSession,
) -> None:
    order, conversation_id = await insert_order_and_conversation(session)
    other_order = new_order()
    await insert_orders(session, [other_order])

    await assert_refused(
        session,
        "fk_approval_requests_conversation_matches_customer_order",
        lambda: insert_approval(
            session,
            other_order,
            conversation_id,
            customer_id=order.customer_id,
            sandbox_id=order.sandbox_id,
        ),
    )


async def test_second_open_handoff_in_the_same_conversation_is_refused(
    session: AsyncSession,
) -> None:
    order, conversation_id = await insert_order_and_conversation(session)
    await insert_handoff(session, order, conversation_id)

    await assert_refused(
        session,
        "uq_handoffs_open_conversation",
        lambda: insert_handoff(session, order, conversation_id),
    )


async def test_new_handoff_is_accepted_once_the_previous_one_is_assigned(
    session: AsyncSession,
) -> None:
    order, conversation_id = await insert_order_and_conversation(session)
    await insert_handoff(
        session,
        order,
        conversation_id,
        status="assigned",
        assigned_to=new_id(),
        assigned_at=order.placed_at,
    )

    await insert_handoff(session, order, conversation_id)


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"priority": "urgent"}, "ck_handoffs_priority_known"),
        ({"status": "closed"}, "ck_handoffs_(status_known|assignment_complete)"),
        ({"status": "assigned"}, "ck_handoffs_assignment_complete"),
        ({"assigned_to": new_id()}, "ck_handoffs_assignment_complete"),
    ],
    ids=["unknown-priority", "unknown-status", "assigned-without-reviewer", "open-with-reviewer"],
)
async def test_inconsistent_handoff_is_refused(
    session: AsyncSession, overrides: dict[str, Any], constraint: str
) -> None:
    order, conversation_id = await insert_order_and_conversation(session)

    await assert_refused(
        session, constraint, lambda: insert_handoff(session, order, conversation_id, **overrides)
    )


async def test_handoff_of_a_customer_other_than_the_conversations_is_refused(
    session: AsyncSession,
) -> None:
    _, conversation_id = await insert_order_and_conversation(session)
    stranger_order = new_order()
    await insert_orders(session, [stranger_order])

    await assert_refused(
        session,
        "fk_handoffs_conversation_matches_customer",
        lambda: insert_handoff(session, stranger_order, conversation_id),
    )


async def test_handoff_priority_rank_is_generated_from_the_priority(
    session: AsyncSession,
) -> None:
    order, conversation_id = await insert_order_and_conversation(session)
    other_conversation = await insert_conversation(session, order)
    normal = await insert_handoff(session, order, conversation_id)
    high = await insert_handoff(session, order, other_conversation, priority="high")

    rows = (await session.execute(select(HandoffRow.id, HandoffRow.priority_rank))).all()

    ranks: dict[UUID, int] = {row.id: row.priority_rank for row in rows}
    assert (ranks[normal["id"]], ranks[high["id"]]) == (1, 2)


async def test_conversation_with_an_unknown_status_is_refused(session: AsyncSession) -> None:
    order = new_order()
    await insert_orders(session, [order])

    await assert_refused(
        session,
        "ck_conversations_status_known",
        lambda: insert_conversation(session, order, status="gone"),
    )


async def test_conversation_of_a_customer_from_another_sandbox_is_refused(
    session: AsyncSession,
) -> None:
    order = new_order()
    await insert_orders(session, [order])

    await assert_refused(
        session,
        "fk_conversations_customer_same_sandbox",
        lambda: insert_conversation(session, order, sandbox_id=new_id()),
    )
