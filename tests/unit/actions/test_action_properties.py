"""For any sequence of proposals, what is stored respects the order total and the customer limit."""

import asyncio
from datetime import timedelta

from action_strategies import FIRST_CLAIM, Attempt, sequences
from hypothesis import HealthCheck, given, settings
from support.action_builders import reviewer_approval, scope_of

from deliverius.actions import (
    ActionExecutor,
    Automatic,
    Executed,
    ExecutionRequest,
    InMemoryUnitOfWork,
)
from deliverius.orders import Order
from deliverius.policy import POLICY_LIMITS, ActionKind, ApprovedBy, ClaimReason
from deliverius.shared import ConversationId, FixedClock, Money, new_id

MONEY_BACK = (ActionKind.REFUND, ActionKind.CANCELLATION)


async def run(orders: tuple[Order, ...], steps: tuple[Attempt, ...]) -> InMemoryUnitOfWork:
    uow = InMemoryUnitOfWork(orders)
    clock = FixedClock(FIRST_CLAIM)
    executor = ActionExecutor(uow, clock)
    conversation = ConversationId(new_id())
    scope = scope_of(orders[0])
    for step in steps:
        clock.advance(timedelta(minutes=step.minutes_later))
        order = orders[step.order_index]
        request = ExecutionRequest(
            conversation_id=conversation,
            order_id=order.id,
            proposal=step.proposal,
            claimed_at=clock.now(),
            authority=reviewer_approval() if step.reviewer_approved else Automatic(),
        )
        first = await executor.execute(scope, request)
        again = await executor.execute(scope, request)
        if isinstance(first, Executed):
            assert again == Executed(first.action, replayed=True)
    return uow


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(sequences())
def test_stored_money_back_never_exceeds_the_order_total_and_auto_approvals_the_limit(
    drawn: tuple[tuple[Order, ...], tuple[Attempt, ...]],
) -> None:
    orders, steps = drawn

    uow = asyncio.run(run(orders, steps))

    for order in orders:
        stored = asyncio.run(uow.actions.list_for_order(scope_of(order), order.id))
        money_back = Money.sum(a.proposal.amount for a in stored if a.proposal.kind in MONEY_BACK)
        assert money_back <= order.total
    every_action = [
        action
        for order in orders
        for action in asyncio.run(uow.actions.list_for_order(scope_of(order), order.id))
    ]
    auto_approved = Money.sum(
        action.proposal.amount
        for action in every_action
        if action.approved_by is ApprovedBy.AUTOMATIC
        and action.proposal.reason is not ClaimReason.CUSTOMER_CANCELLATION
    )
    # The attempts span at most 15 days, so the 30-day window covers all of them.
    assert auto_approved <= POLICY_LIMITS.customer_auto_approval_limit
