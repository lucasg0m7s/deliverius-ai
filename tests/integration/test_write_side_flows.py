"""The final states of the agent flows, on the write side, against Postgres.

Each test calls the services in the order the graph will call them (`docs/fluxos-do-agente.md`):
the executor stands in for `check_policy`, since it re-evaluates the policy and stores nothing when
the answer is not "execute". The graph itself arrives later; these states are what it must reach.
"""

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from support.action_builders import (
    EXECUTED_AT,
    delivered_order,
    delivered_order_of,
    refund_of,
    request_for,
    scope_of,
)
from support.action_rows import insert_conversation
from support.approval_builders import REASON, REQUESTED_AT, summary
from support.order_builders import build_item
from support.order_rows import insert_orders

from deliverius.actions import (
    ActionExecutor,
    Executed,
    ExecutedAction,
    NotExecuted,
    PostgresActionStore,
    ReviewerApproved,
)
from deliverius.actions import postgres_unit_of_work as actions_unit_of_work
from deliverius.approvals import (
    ApprovalRequest,
    ApprovalService,
    Decision,
    HandoffService,
    NewApprovalRequest,
    Priority,
)
from deliverius.approvals import postgres_unit_of_work as approvals_unit_of_work
from deliverius.db.session import create_session_factory
from deliverius.db.tables import ApprovalRequestRow, AuditLogRow, HandoffRow
from deliverius.orders import Order
from deliverius.policy import (
    ActionKind,
    ApprovedBy,
    ClaimReason,
    DenialReason,
    Denied,
    NeedsApproval,
    ProposedAction,
)
from deliverius.shared import ConversationId, FixedClock, Money, ReviewerId, SandboxScope, new_id

pytestmark = pytest.mark.integration


class Services:
    def __init__(self, engine: AsyncEngine) -> None:
        factory = create_session_factory(engine)
        self.engine = engine
        self.executor = ActionExecutor(actions_unit_of_work(factory), FixedClock(EXECUTED_AT))
        self.approvals = ApprovalService(approvals_unit_of_work(factory), FixedClock(REQUESTED_AT))
        self.handoffs = HandoffService(approvals_unit_of_work(factory), FixedClock(REQUESTED_AT))

    async def actions_of(self, order: Order) -> list[ExecutedAction]:
        async with AsyncSession(self.engine) as session:
            return list(
                await PostgresActionStore(session).list_for_order(scope_of(order), order.id)
            )

    async def audit_of(self, order: Order) -> list[AuditLogRow]:
        async with AsyncSession(self.engine) as session:
            statement = (
                select(AuditLogRow)
                .where(AuditLogRow.sandbox_id == order.sandbox_id)
                .order_by(AuditLogRow.occurred_at, AuditLogRow.action)
            )
            return list((await session.execute(statement)).scalars())

    async def count(self, table: Any, order: Order) -> int:
        async with AsyncSession(self.engine) as session:
            rows = await session.execute(
                select(table.id).where(table.sandbox_id == order.sandbox_id)
            )
            return len(rows.all())


@pytest.fixture
def services(engine: AsyncEngine) -> Services:
    return Services(engine)


async def commit(engine: AsyncEngine, *orders: Order) -> ConversationId:
    """The orders, and one conversation about the first."""
    async with AsyncSession(engine) as session, session.begin():
        await insert_orders(session, orders)
        return ConversationId(await insert_conversation(session, orders[0]))


async def test_f1_missing_item_is_refunded_automatically(services: Services) -> None:
    refri = build_item("Refrigerante", 800)
    order = delivered_order(build_item("Pizza", 5_990), refri)
    conversation = await commit(services.engine, order)

    result = await services.executor.execute(
        scope_of(order), request_for(order, conversation, refund_of(refri, 800))
    )

    assert isinstance(result, Executed)
    [action] = await services.actions_of(order)
    assert (action.proposal.amount, action.proposal.items[0].order_item_id) == (
        Money(800),
        refri.id,
    )
    assert action.approved_by is ApprovedBy.AUTOMATIC
    assert [entry.action for entry in await services.audit_of(order)] == ["actions.executed"]
    assert await services.count(ApprovalRequestRow, order) == 0


async def f2_until_the_decision(
    services: Services, decision: Decision
) -> tuple[Order, ConversationId, ProposedAction, ApprovalRequest]:
    """F2 up to the reviewer's decision: three recent refunds, then R$ 186,00 "never arrived"."""
    order = delivered_order(build_item("Combo família", 18_600))
    recent = [delivered_order_of(order, 1_000) for _ in range(3)]
    conversation = await commit(services.engine, order, *recent)
    for previous in recent:
        previous_conversation = await commit_conversation(services.engine, previous)
        await services.executor.execute(
            scope_of(previous),
            request_for(previous, previous_conversation, refund_of(previous.items[0], 1_000)),
        )
    proposal = ProposedAction(
        kind=ActionKind.REFUND, reason=ClaimReason.NOT_DELIVERED, amount=Money(18_600)
    )
    checked = await services.executor.execute(
        scope_of(order), request_for(order, conversation, proposal)
    )
    assert isinstance(checked, NotExecuted)
    assert isinstance(checked.decision, NeedsApproval)
    request = await services.approvals.request(
        scope_of(order),
        NewApprovalRequest(
            conversation_id=conversation,
            order_id=order.id,
            proposal=proposal,
            claimed_at=request_for(order, conversation, proposal).claimed_at,
            decision=checked.decision,
        ),
    )
    decided = await services.approvals.decide(
        SandboxScope(order.sandbox_id),
        request.id,
        expected_version=request.version,
        decision=decision,
        reason=REASON,
        reviewer_id=ReviewerId(new_id()),
    )
    assert decided.status is decision.status
    return order, conversation, proposal, decided


async def commit_conversation(engine: AsyncEngine, order: Order) -> ConversationId:
    async with AsyncSession(engine) as session, session.begin():
        return ConversationId(await insert_conversation(session, order))


async def test_f2a_approved_refund_is_executed_once_with_the_reviewer(
    services: Services,
) -> None:
    order, conversation, proposal, approval = await f2_until_the_decision(
        services, Decision.APPROVED
    )
    reviewer = approval.decided_by
    assert reviewer is not None
    authority = ReviewerApproved(approval.id, reviewer)

    first = await services.executor.execute(
        scope_of(order), request_for(order, conversation, proposal, authority)
    )
    second = await services.executor.execute(
        scope_of(order), request_for(order, conversation, proposal, authority)
    )

    assert isinstance(first, Executed)
    assert isinstance(second, Executed)
    assert second.replayed is True
    [action] = await services.actions_of(order)
    assert (action.proposal.amount, action.approved_by, action.approval_request_id) == (
        Money(18_600),
        ApprovedBy.REVIEWER,
        approval.id,
    )
    audit = await services.audit_of(order)
    [decided] = [entry for entry in audit if entry.action == "approvals.decided"]
    [executed] = [entry for entry in audit if entry.target_id == action.id]
    assert decided.actor_id == reviewer
    assert executed.data["reviewer_id"] == str(reviewer)


async def test_f2b_rejected_refund_leaves_no_refund_and_audits_the_rejection(
    services: Services,
) -> None:
    order, _, _, rejection = await f2_until_the_decision(services, Decision.REJECTED)
    reviewer = rejection.decided_by

    assert await services.actions_of(order) == []
    audit = await services.audit_of(order)
    [decided] = [entry for entry in audit if entry.action == "approvals.decided"]
    assert decided.actor_id == reviewer
    assert decided.data == {"decision": "rejected", "version": 2}
    # Only the three refunds of the history were executed.
    assert [entry.action for entry in audit].count("actions.executed") == 3


async def test_f7_denied_full_refund_then_the_item_value_is_executed(
    services: Services,
) -> None:
    drink = build_item("Bebida", 1_200)
    order = delivered_order(build_item("Lanche", 6_200), drink)
    conversation = await commit(services.engine, order)
    full = ProposedAction(
        kind=ActionKind.REFUND,
        reason=ClaimReason.WRONG_ITEM,
        amount=Money(7_400),
        items=refund_of(drink, 1_200).items,
    )

    denied = await services.executor.execute(
        scope_of(order), request_for(order, conversation, full)
    )
    revised = await services.executor.execute(
        scope_of(order),
        request_for(
            order,
            conversation,
            ProposedAction(
                kind=ActionKind.REFUND,
                reason=ClaimReason.WRONG_ITEM,
                amount=Money(1_200),
                items=full.items,
            ),
        ),
    )

    assert isinstance(denied, NotExecuted)
    assert isinstance(denied.decision, Denied)
    assert denied.decision.reason is DenialReason.EXCEEDS_CEILING
    assert isinstance(revised, Executed)
    [action] = await services.actions_of(order)
    assert action.proposal.amount == Money(1_200)


async def test_f5_allergy_is_handed_off_with_high_priority_and_no_action(
    services: Services,
) -> None:
    order = delivered_order()
    conversation = await commit(services.engine, order)

    handoff = await services.handoffs.create(
        scope_of(order), conversation, Priority.HIGH, summary("cliente relata mal-estar")
    )

    assert handoff.priority is Priority.HIGH
    assert await services.count(HandoffRow, order) == 1
    assert await services.actions_of(order) == []
    assert await services.count(ApprovalRequestRow, order) == 0
