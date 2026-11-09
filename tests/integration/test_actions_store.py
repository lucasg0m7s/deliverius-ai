from dataclasses import replace
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from support.action_builders import (
    delivered_order,
    executed_action,
    refund_of,
    scope_of,
)
from support.action_rows import (
    decision_values,
    insert_approval,
    insert_conversation,
    insert_order_and_conversation,
)
from support.action_store_contract import ActionStoreContract, StoreHarness
from support.order_rows import insert_orders

from deliverius.actions import PostgresActionStore, ReviewerApproved
from deliverius.db.errors import DatabaseUnavailableError
from deliverius.db.tables import ActionRow
from deliverius.orders import Order
from deliverius.policy import ApprovedBy, proposal_key
from deliverius.shared import (
    ApprovalRequestId,
    ConflictError,
    ConversationId,
    Money,
    ReviewerId,
    new_id,
)

pytestmark = pytest.mark.integration


class PostgresHarness:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self.store = PostgresActionStore(session)
        self._conversations: dict[Order, ConversationId] = {}

    async def add_order(self, order: Order) -> ConversationId:
        if order not in self._conversations:
            await insert_orders(self._session, [order])
            conversation_id = await insert_conversation(self._session, order)
            self._conversations[order] = ConversationId(conversation_id)
        return self._conversations[order]


class TestPostgresActionStore(ActionStoreContract):
    @pytest.fixture
    def harness(self, session: AsyncSession) -> StoreHarness:
        return PostgresHarness(session)


async def stored_count(session: AsyncSession, order: Order) -> int:
    statement = select(func.count()).select_from(ActionRow).where(ActionRow.order_id == order.id)
    return await session.scalar(statement) or 0


async def test_action_approved_by_a_reviewer_is_stored_with_its_approval(
    session: AsyncSession,
) -> None:
    order, conversation_id = await insert_order_and_conversation(session)
    proposal = refund_of(order.items[0], 800)
    key = proposal_key(ConversationId(conversation_id), order.id, proposal)
    approval = await insert_approval(
        session, order, conversation_id, proposal_key=key, **decision_values()
    )
    action = executed_action(
        order,
        ConversationId(conversation_id),
        proposal,
        approval=approved_by_reviewer(approval),
    )
    store = PostgresActionStore(session)

    assert await store.insert(action) == action

    found = await store.find_by_key(scope_of(order), key)
    assert found == action
    assert found is not None
    assert found.approved_by is ApprovedBy.REVIEWER


def approved_by_reviewer(approval: dict[str, Any]) -> ReviewerApproved:
    return ReviewerApproved(ApprovalRequestId(approval["id"]), ReviewerId(new_id()))


async def test_action_pointing_at_a_pending_approval_is_a_conflict_and_the_session_stays_usable(
    session: AsyncSession,
) -> None:
    order, conversation_id = await insert_order_and_conversation(session)
    proposal = refund_of(order.items[0], 800)
    key = proposal_key(ConversationId(conversation_id), order.id, proposal)
    pending = await insert_approval(session, order, conversation_id, proposal_key=key)
    store = PostgresActionStore(session)
    mismatched = executed_action(
        order,
        ConversationId(conversation_id),
        proposal,
        approval=approved_by_reviewer(pending),
    )

    with pytest.raises(ConflictError) as error:
        await store.insert(mismatched)

    assert error.value.code == "action_approval_mismatch"
    assert await stored_count(session, order) == 0
    fine = executed_action(order, ConversationId(conversation_id), refund_of(order.items[0], 500))
    assert await store.insert(fine) == fine


async def test_action_of_an_order_other_than_the_conversations_is_a_conflict(
    session: AsyncSession,
) -> None:
    order, conversation_id = await insert_order_and_conversation(session)
    other = replace(
        delivered_order(),
        customer_id=order.customer_id,
        sandbox_id=order.sandbox_id,
    )
    await insert_orders(session, [other])
    action = executed_action(other, ConversationId(conversation_id), refund_of(other.items[0], 500))

    with pytest.raises(ConflictError) as error:
        await PostgresActionStore(session).insert(action)

    assert error.value.code == "action_conversation_mismatch"
    assert await stored_count(session, other) == 0


async def test_any_other_refused_row_is_a_database_error_not_a_conflict(
    session: AsyncSession,
) -> None:
    order, conversation_id = await insert_order_and_conversation(session)
    action = executed_action(order, ConversationId(conversation_id), refund_of(order.items[0], 500))
    zero = replace(action, proposal=replace(action.proposal, amount=Money(0)))

    with pytest.raises(DatabaseUnavailableError):
        await PostgresActionStore(session).insert(zero)

    assert await stored_count(session, order) == 0
