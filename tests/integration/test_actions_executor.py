"""The executor against Postgres: races, a cancelled task and the scope.

The data is committed so separate transactions can contend for it. Random sandbox IDs keep tests
apart, and nothing is cleaned up: decided approvals and audit entries cannot be deleted by design.
"""

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from support.action_builders import (
    EXECUTED_AT,
    cancellation_of,
    delivered_order,
    delivered_order_of,
    refund_of,
    request_for,
    scope_of,
)
from support.action_rows import decision_values, insert_approval, insert_conversation
from support.order_builders import build_item, build_order_in
from support.order_rows import insert_orders
from support.policy_builders import DELIVERED_AT

from deliverius.actions import (
    ActionExecutor,
    Executed,
    ExecutedAction,
    ExecutionResult,
    ExecutionUnit,
    NotExecuted,
    PostgresActionStore,
    ReviewerApproved,
    UnitOfWork,
    postgres_unit_of_work,
)
from deliverius.db.session import create_session_factory
from deliverius.db.tables import AuditLogRow, DeliveryEventRow, OrderRow
from deliverius.orders import Order, OrderStatus
from deliverius.policy import ApprovalReason, DenialReason, Denied, NeedsApproval, proposal_key
from deliverius.shared import (
    ApprovalRequestId,
    AuditEntry,
    AuditSink,
    ConversationId,
    CustomerId,
    CustomerScope,
    FixedClock,
    Money,
    NotFoundError,
    ReviewerId,
    SandboxId,
    new_id,
)

pytestmark = pytest.mark.integration

ROUNDS = 20


@dataclass(frozen=True)
class Claim:
    """An order and the conversation a claim about it is made in."""

    order: Order
    conversation: ConversationId


def executor_on(engine: AsyncEngine) -> ActionExecutor:
    return ActionExecutor(
        postgres_unit_of_work(create_session_factory(engine)), FixedClock(EXECUTED_AT)
    )


async def commit_claims(engine: AsyncEngine, orders: Sequence[Order]) -> list[Claim]:
    """The orders and one conversation each, committed."""
    async with AsyncSession(engine) as session, session.begin():
        await insert_orders(session, orders)
        return [
            Claim(order, ConversationId(await insert_conversation(session, order)))
            for order in orders
        ]


async def commit_conversation(engine: AsyncEngine, order: Order) -> ConversationId:
    async with AsyncSession(engine) as session, session.begin():
        return ConversationId(await insert_conversation(session, order))


async def commit_approval(engine: AsyncEngine, claim: Claim, key: str) -> ReviewerApproved:
    async with AsyncSession(engine) as session, session.begin():
        values = await insert_approval(
            session, claim.order, claim.conversation, proposal_key=key, **decision_values()
        )
    return ReviewerApproved(ApprovalRequestId(values["id"]), ReviewerId(new_id()))


async def stored_actions(engine: AsyncEngine, *orders: Order) -> list[ExecutedAction]:
    async with AsyncSession(engine) as session:
        found: list[ExecutedAction] = []
        for order in orders:
            found += await PostgresActionStore(session).list_for_order(scope_of(order), order.id)
        return found


async def audit_entries_of(engine: AsyncEngine, sandbox_id: SandboxId) -> int:
    async with AsyncSession(engine) as session:
        statement = (
            select(func.count())
            .select_from(AuditLogRow)
            .where(AuditLogRow.sandbox_id == sandbox_id, AuditLogRow.action == "actions.executed")
        )
        return await session.scalar(statement) or 0


def executed(results: Sequence[ExecutionResult]) -> list[Executed]:
    return [result for result in results if isinstance(result, Executed)]


def refused(results: Sequence[ExecutionResult]) -> list[NotExecuted]:
    return [result for result in results if isinstance(result, NotExecuted)]


def tight_order(first_cents: int, second_cents: int, discount_cents: int) -> Order:
    """A delivered order of two items whose total is lower than the sum of the two items."""
    return build_order_in(
        OrderStatus.DELIVERED,
        (build_item("A", first_cents), build_item("B", second_cents)),
        final_event_at=DELIVERED_AT,
        discount=Money(discount_cents),
    )


async def test_same_proposal_executed_together_stores_one_action_and_one_audit_entry(
    engine: AsyncEngine,
) -> None:
    for _ in range(ROUNDS):
        [claim] = await commit_claims(engine, [delivered_order(build_item("Suco", 800))])
        request = request_for(claim.order, claim.conversation, refund_of(claim.order.items[0], 800))
        executor = executor_on(engine)

        results = await asyncio.gather(
            executor.execute(scope_of(claim.order), request),
            executor.execute(scope_of(claim.order), request),
        )

        done = executed(results)
        assert len(done) == 2
        assert sorted(result.replayed for result in done) == [False, True]
        assert done[0].action == done[1].action
        assert len(await stored_actions(engine, claim.order)) == 1
        assert await audit_entries_of(engine, claim.order.sandbox_id) == 1


async def test_different_refunds_together_that_pass_the_ceiling_store_only_one(
    engine: AsyncEngine,
) -> None:
    for _ in range(ROUNDS):
        order = tight_order(4_000, 4_000, discount_cents=2_000)
        [claim] = await commit_claims(engine, [order])
        other_conversation = await commit_conversation(engine, order)
        executor = executor_on(engine)

        results = await asyncio.gather(
            executor.execute(
                scope_of(order),
                request_for(order, claim.conversation, refund_of(order.items[0], 4_000)),
            ),
            executor.execute(
                scope_of(order),
                request_for(order, other_conversation, refund_of(order.items[1], 4_000)),
            ),
        )

        [loser] = refused(results)
        assert len(executed(results)) == 1
        assert isinstance(loser.decision, Denied)
        assert loser.decision.reason is DenialReason.EXCEEDS_CEILING
        stored = await stored_actions(engine, order)
        assert sum(action.proposal.amount.cents for action in stored) <= order.total.cents


async def test_automatic_refunds_together_on_two_orders_that_pass_the_customer_limit_store_one(
    engine: AsyncEngine,
) -> None:
    for _ in range(ROUNDS):
        history_order = delivered_order(build_item("X", 3_000), build_item("Y", 3_000))
        first = delivered_order_of(history_order, 9_000)
        second = delivered_order_of(history_order, 9_000)
        seed, claim_one, claim_two = await commit_claims(engine, [history_order, first, second])
        executor = executor_on(engine)
        for item in history_order.items:
            seeded = await executor.execute(
                scope_of(history_order),
                request_for(history_order, seed.conversation, refund_of(item, 3_000)),
            )
            assert isinstance(seeded, Executed)

        results = await asyncio.gather(
            *(
                executor.execute(
                    scope_of(claim.order),
                    request_for(
                        claim.order, claim.conversation, refund_of(claim.order.items[0], 4_000)
                    ),
                )
                for claim in (claim_one, claim_two)
            )
        )

        [loser] = refused(results)
        assert len(executed(results)) == 1
        assert isinstance(loser.decision, NeedsApproval)
        assert loser.decision.reasons == (ApprovalReason.EXCEEDS_CUSTOMER_LIMIT,)
        stored = await stored_actions(engine, history_order, first, second)
        assert sum(action.proposal.amount.cents for action in stored) == 10_000


def approved_world_order() -> Order:
    """Total 8,000: a 6,000 refund of A (needs a reviewer) and a 3,000 one of B cannot both fit."""
    return tight_order(7_000, 3_000, discount_cents=2_000)


async def test_approved_refund_resumed_while_another_one_leaves_is_denied_by_the_reevaluation(
    engine: AsyncEngine,
) -> None:
    for _ in range(ROUNDS):
        order = approved_world_order()
        [claim] = await commit_claims(engine, [order])
        other_conversation = await commit_conversation(engine, order)
        approved = refund_of(order.items[0], 6_000)
        approval = await commit_approval(
            engine, claim, proposal_key(claim.conversation, order.id, approved)
        )
        executor = executor_on(engine)

        results = await asyncio.gather(
            executor.execute(
                scope_of(order), request_for(order, claim.conversation, approved, approval)
            ),
            executor.execute(
                scope_of(order),
                request_for(order, other_conversation, refund_of(order.items[1], 3_000)),
            ),
        )

        [loser] = refused(results)
        assert len(executed(results)) == 1
        assert isinstance(loser.decision, Denied)
        assert loser.decision.reason is DenialReason.EXCEEDS_CEILING


async def test_approval_decided_before_another_refund_left_is_denied_when_resumed(
    engine: AsyncEngine,
) -> None:
    order = approved_world_order()
    [claim] = await commit_claims(engine, [order])
    other_conversation = await commit_conversation(engine, order)
    approved = refund_of(order.items[0], 6_000)
    approval = await commit_approval(
        engine, claim, proposal_key(claim.conversation, order.id, approved)
    )
    executor = executor_on(engine)
    other = await executor.execute(
        scope_of(order), request_for(order, other_conversation, refund_of(order.items[1], 3_000))
    )

    resumed = await executor.execute(
        scope_of(order), request_for(order, claim.conversation, approved, approval)
    )

    assert isinstance(other, Executed)
    assert isinstance(resumed, NotExecuted)
    assert isinstance(resumed.decision, Denied)
    assert resumed.decision.reason is DenialReason.EXCEEDS_CEILING
    assert [a.proposal.amount for a in await stored_actions(engine, order)] == [Money(3_000)]


async def test_approved_refund_is_executed_with_its_approval(engine: AsyncEngine) -> None:
    order = approved_world_order()
    [claim] = await commit_claims(engine, [order])
    approved = refund_of(order.items[0], 6_000)
    approval = await commit_approval(
        engine, claim, proposal_key(claim.conversation, order.id, approved)
    )

    result = await executor_on(engine).execute(
        scope_of(order), request_for(order, claim.conversation, approved, approval)
    )

    assert isinstance(result, Executed)
    assert result.action.approval_request_id == approval.approval_request_id
    [stored] = await stored_actions(engine, order)
    assert stored == result.action


class StallingAudit:
    """Records the entry, then waits forever: the task is cancelled between insert and commit."""

    def __init__(self, inner: AuditSink, reached: asyncio.Event) -> None:
        self._inner = inner
        self._reached = reached

    async def record(self, entry: AuditEntry) -> None:
        await self._inner.record(entry)
        self._reached.set()
        await asyncio.Event().wait()


class StallingUnit:
    def __init__(self, inner: ExecutionUnit, reached: asyncio.Event) -> None:
        self.orders = inner.orders
        self.actions = inner.actions
        self.audit = StallingAudit(inner.audit, reached)


def stalling_unit_of_work(engine: AsyncEngine, reached: asyncio.Event) -> UnitOfWork:
    real = postgres_unit_of_work(create_session_factory(engine))

    @asynccontextmanager
    async def open_unit() -> AsyncIterator[ExecutionUnit]:
        async with real() as unit:
            yield StallingUnit(unit, reached)

    return open_unit


async def test_task_cancelled_before_the_commit_leaves_no_action_audit_or_cancellation(
    engine: AsyncEngine,
) -> None:
    order = build_order_in(OrderStatus.PLACED, (build_item("Pizza", 4_000),))
    [claim] = await commit_claims(engine, [order])
    reached = asyncio.Event()
    stalled = ActionExecutor(stalling_unit_of_work(engine, reached), FixedClock(EXECUTED_AT))
    request = request_for(order, claim.conversation, cancellation_of(order))
    task = asyncio.create_task(stalled.execute(scope_of(order), request))

    async with asyncio.timeout(10):
        await reached.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert await stored_actions(engine, order) == []
    assert await audit_entries_of(engine, order.sandbox_id) == 0
    async with AsyncSession(engine) as session:
        status = await session.scalar(select(OrderRow.status).where(OrderRow.id == order.id))
        events = await session.scalar(
            select(func.count())
            .select_from(DeliveryEventRow)
            .where(DeliveryEventRow.order_id == order.id)
        )
    assert status == OrderStatus.PLACED.value
    assert events == len(order.events)
    again = await executor_on(engine).execute(scope_of(order), request)
    assert isinstance(again, Executed)


async def test_order_of_another_customer_or_sandbox_is_not_found_and_nothing_is_written(
    engine: AsyncEngine,
) -> None:
    [claim] = await commit_claims(engine, [delivered_order(build_item("Suco", 800))])
    order = claim.order
    request = request_for(order, claim.conversation, refund_of(order.items[0], 800))
    neighbour = CustomerScope(order.sandbox_id, CustomerId(new_id()))
    elsewhere = CustomerScope(SandboxId(new_id()), order.customer_id)
    executor = executor_on(engine)

    for scope in (neighbour, elsewhere):
        with pytest.raises(NotFoundError):
            await executor.execute(scope, request)

    assert await stored_actions(engine, order) == []
    assert await audit_entries_of(engine, order.sandbox_id) == 0


async def test_actions_of_an_order_are_listed_only_inside_the_scope(engine: AsyncEngine) -> None:
    [claim] = await commit_claims(engine, [delivered_order(build_item("Suco", 800))])
    order = claim.order
    done = await executor_on(engine).execute(
        scope_of(order),
        request_for(order, claim.conversation, refund_of(order.items[0], 800)),
    )
    assert isinstance(done, Executed)

    async with AsyncSession(engine) as session:
        store = PostgresActionStore(session)
        neighbour = CustomerScope(order.sandbox_id, CustomerId(new_id()))
        assert await store.list_for_order(scope_of(order), order.id) == (done.action,)
        assert await store.list_for_order(neighbour, order.id) == ()
