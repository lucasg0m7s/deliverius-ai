"""Approvals and handoffs against Postgres: races, scope, queues and the conversation status.

The data is committed so separate transactions can contend for it. Random sandbox IDs keep tests
apart, and nothing is cleaned up: decided approvals and audit entries cannot be deleted by design.
"""

import asyncio
from collections.abc import Sequence
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from support.action_builders import CLAIMED_AT, delivered_order
from support.action_rows import insert_conversation
from support.approval_builders import (
    HIGH,
    NORMAL,
    REASON,
    REQUESTED_AT,
    big_refund,
    customer_scope,
    fraud_assessment,
    needs_approval,
    new_request,
    reviewer_scope,
    summary,
)
from support.order_rows import insert_orders
from support.sql_recorder import StatementRecorder

from deliverius.approvals import (
    ApprovalRequest,
    ApprovalService,
    ApprovalStatus,
    Decision,
    Handoff,
    HandoffService,
    HandoffStatus,
    PostgresApprovalStore,
    PostgresHandoffStore,
    Priority,
    postgres_unit_of_work,
)
from deliverius.db.session import create_session_factory
from deliverius.db.tables import ApprovalRequestRow, AuditLogRow, ConversationRow
from deliverius.orders import Order, PageRequest
from deliverius.policy import proposal_key
from deliverius.shared import (
    ApprovalRequestId,
    ConflictError,
    ConversationId,
    CustomerId,
    CustomerScope,
    FixedClock,
    HandoffId,
    NotFoundError,
    ReviewerId,
    SandboxId,
    SandboxScope,
    new_id,
)

pytestmark = pytest.mark.integration

ROUNDS = 20


def approvals_on(engine: AsyncEngine, minutes: int = 0) -> ApprovalService:
    clock = FixedClock(REQUESTED_AT + timedelta(minutes=minutes))
    return ApprovalService(postgres_unit_of_work(create_session_factory(engine)), clock)


def handoffs_on(engine: AsyncEngine, minutes: int = 0) -> HandoffService:
    clock = FixedClock(REQUESTED_AT + timedelta(minutes=minutes))
    return HandoffService(postgres_unit_of_work(create_session_factory(engine)), clock)


async def commit_order(
    engine: AsyncEngine, conversations: int = 1
) -> tuple[Order, list[ConversationId]]:
    """A delivered order of a fresh sandbox and `conversations` conversations about it."""
    order = delivered_order()
    async with AsyncSession(engine) as session, session.begin():
        await insert_orders(session, [order])
        ids = [
            ConversationId(await insert_conversation(session, order)) for _ in range(conversations)
        ]
    return order, ids


async def commit_other_customer(engine: AsyncEngine, order: Order) -> CustomerScope:
    """An existing customer of the same sandbox, with an order of their own."""
    other = replace(delivered_order(), sandbox_id=order.sandbox_id)
    async with AsyncSession(engine) as session, session.begin():
        await insert_orders(session, [other])
    return customer_scope(other)


async def conversation_status(engine: AsyncEngine, conversation: ConversationId) -> str:
    async with AsyncSession(engine) as session:
        statement = select(ConversationRow.status).where(ConversationRow.id == conversation)
        return (await session.execute(statement)).scalar_one()


async def audit_count(engine: AsyncEngine, sandbox_id: SandboxId, action: str) -> int:
    async with AsyncSession(engine) as session:
        statement = (
            select(func.count())
            .select_from(AuditLogRow)
            .where(AuditLogRow.sandbox_id == sandbox_id, AuditLogRow.action == action)
        )
        return await session.scalar(statement) or 0


async def request_count(engine: AsyncEngine, conversation: ConversationId) -> int:
    async with AsyncSession(engine) as session:
        statement = (
            select(func.count())
            .select_from(ApprovalRequestRow)
            .where(ApprovalRequestRow.conversation_id == conversation)
        )
        return await session.scalar(statement) or 0


async def decide(
    service: ApprovalService,
    order: Order,
    request: ApprovalRequest,
    decision: Decision,
    *,
    version: int = 1,
) -> ApprovalRequest:
    return await service.decide(
        reviewer_scope(order),
        request.id,
        expected_version=version,
        decision=decision,
        reason=REASON,
        reviewer_id=ReviewerId(new_id()),
    )


def conflicts(results: Sequence[object]) -> list[str]:
    return [result.code for result in results if isinstance(result, ConflictError)]


def unexpected(results: Sequence[object]) -> list[BaseException]:
    return [
        result
        for result in results
        if isinstance(result, BaseException) and not isinstance(result, ConflictError)
    ]


class TestApprovalRequests:
    async def test_round_trip_keeps_every_field(self, engine: AsyncEngine) -> None:
        order, [conversation] = await commit_order(engine)
        service = approvals_on(engine)
        fraud = fraud_assessment()

        created = await service.request(
            customer_scope(order), new_request(order, conversation, fraud=fraud)
        )

        assert await service.get(customer_scope(order), created.id) == created
        assert created.fraud == fraud

    async def test_conversation_status_follows_request_and_decision(
        self, engine: AsyncEngine
    ) -> None:
        order, [conversation] = await commit_order(engine)
        service = approvals_on(engine)
        assert await conversation_status(engine, conversation) == "open"

        request = await service.request(customer_scope(order), new_request(order, conversation))
        assert await conversation_status(engine, conversation) == "awaiting_approval"

        decided = await decide(service, order, request, Decision.APPROVED)
        assert await conversation_status(engine, conversation) == "open"
        assert (decided.status, decided.version, decided.decision_reason) == (
            ApprovalStatus.APPROVED,
            2,
            REASON,
        )
        assert await audit_count(engine, order.sandbox_id, "approvals.requested") == 1
        assert await audit_count(engine, order.sandbox_id, "approvals.decided") == 1

    async def test_conversation_of_another_customer_conflicts(self, engine: AsyncEngine) -> None:
        order, [conversation] = await commit_order(engine)
        stranger = await commit_other_customer(engine, order)

        with pytest.raises(ConflictError) as raised:
            await approvals_on(engine).request(stranger, new_request(order, conversation))

        assert raised.value.code == "approval_conversation_mismatch"
        assert await request_count(engine, conversation) == 0

    async def test_stale_version_on_a_pending_request_conflicts(self, engine: AsyncEngine) -> None:
        order, [conversation] = await commit_order(engine)
        service = approvals_on(engine)
        request = await service.request(customer_scope(order), new_request(order, conversation))
        async with AsyncSession(engine) as session, session.begin():
            await session.execute(
                update(ApprovalRequestRow)
                .where(ApprovalRequestRow.id == request.id)
                .values(version=2)
            )

        with pytest.raises(ConflictError) as raised:
            await decide(service, order, request, Decision.APPROVED, version=1)

        assert raised.value.code == "approval_version_stale"
        current = await service.get(reviewer_scope(order), request.id)
        assert (current.status, current.version) == (ApprovalStatus.PENDING, 2)


class TestApprovalRaces:
    async def test_opposite_decisions_together_record_one(self, engine: AsyncEngine) -> None:
        for _ in range(ROUNDS):
            order, [conversation] = await commit_order(engine)
            service = approvals_on(engine)
            request = await service.request(customer_scope(order), new_request(order, conversation))

            results = await asyncio.gather(
                decide(service, order, request, Decision.APPROVED),
                decide(service, order, request, Decision.REJECTED),
                return_exceptions=True,
            )

            assert unexpected(results) == []
            assert conflicts(results) == ["approval_already_decided"]
            [winner] = [result for result in results if isinstance(result, ApprovalRequest)]
            assert await service.get(reviewer_scope(order), request.id) == winner
            assert await audit_count(engine, order.sandbox_id, "approvals.decided") == 1

    async def test_same_decision_together_returns_the_approved_state_to_both(
        self, engine: AsyncEngine
    ) -> None:
        for _ in range(ROUNDS):
            order, [conversation] = await commit_order(engine)
            service = approvals_on(engine)
            request = await service.request(customer_scope(order), new_request(order, conversation))

            first, second = await asyncio.gather(
                decide(service, order, request, Decision.APPROVED),
                decide(service, order, request, Decision.APPROVED),
            )

            assert first == second
            assert first.status is ApprovalStatus.APPROVED
            assert await audit_count(engine, order.sandbox_id, "approvals.decided") == 1

    async def test_same_request_created_together_stores_one(self, engine: AsyncEngine) -> None:
        for _ in range(ROUNDS):
            order, [conversation] = await commit_order(engine)
            service = approvals_on(engine)
            new = new_request(order, conversation)

            first, second = await asyncio.gather(
                service.request(customer_scope(order), new),
                service.request(customer_scope(order), new),
            )

            assert first.id == second.id
            assert await request_count(engine, conversation) == 1
            assert await audit_count(engine, order.sandbox_id, "approvals.requested") == 1

    async def test_two_proposals_together_leave_one_pending(self, engine: AsyncEngine) -> None:
        for _ in range(ROUNDS):
            order, [conversation] = await commit_order(engine)
            service = approvals_on(engine)

            results = await asyncio.gather(
                service.request(customer_scope(order), new_request(order, conversation)),
                service.request(
                    customer_scope(order),
                    new_request(order, conversation, big_refund(order, 9_000)),
                ),
                return_exceptions=True,
            )

            assert unexpected(results) == []
            assert conflicts(results) == ["approval_already_pending"]
            assert await request_count(engine, conversation) == 1


class TestApprovalScopeAndQueue:
    async def test_other_sandbox_and_other_customer_get_not_found(
        self, engine: AsyncEngine
    ) -> None:
        order, [conversation] = await commit_order(engine)
        service = approvals_on(engine)
        request = await service.request(customer_scope(order), new_request(order, conversation))

        with pytest.raises(NotFoundError):
            await service.get(CustomerScope(order.sandbox_id, CustomerId(new_id())), request.id)
        with pytest.raises(NotFoundError):
            await service.get(SandboxScope(SandboxId(new_id())), request.id)
        with pytest.raises(NotFoundError):
            await service.decide(
                SandboxScope(SandboxId(new_id())),
                request.id,
                expected_version=1,
                decision=Decision.APPROVED,
                reason=REASON,
                reviewer_id=ReviewerId(new_id()),
            )

    async def test_sixty_pending_come_in_pages_of_fifty_and_ten(self, engine: AsyncEngine) -> None:
        order, conversations = await commit_order(engine, conversations=60)
        for minute, conversation in enumerate(conversations):
            await approvals_on(engine, minute).request(
                customer_scope(order), new_request(order, conversation)
            )
        service = approvals_on(engine)
        scope = reviewer_scope(order)

        first = await service.pending_queue(scope, PageRequest())
        second = await service.pending_queue(scope, PageRequest(cursor=first.next_cursor))

        assert (len(first.items), len(second.items), second.next_cursor) == (50, 10, None)
        seen = [request.conversation_id for request in first.items + second.items]
        assert seen == conversations

    async def test_pending_queue_walks_the_partial_index(self, session: AsyncSession) -> None:
        order = delivered_order()
        await insert_orders(session, [order])
        store = PostgresApprovalStore(session)
        for minute in range(30):
            conversation = ConversationId(await insert_conversation(session, order))
            request = pending_at(order, conversation, minute)
            await store.create(customer_scope(order), request)
        await session.execute(text("ANALYZE approval_requests"))

        with StatementRecorder(session) as recorder:
            page = await store.pending_queue(reviewer_scope(order), PageRequest(limit=10))

        assert len(page.items) == 10
        plan = await explain(session, recorder)
        assert "ix_approval_requests_pending_queue" in plan, plan
        assert "Seq Scan" not in plan, plan


class TestHandoffs:
    async def test_creation_transfers_the_conversation(self, engine: AsyncEngine) -> None:
        order, [conversation] = await commit_order(engine)
        service = handoffs_on(engine)

        handoff = await service.create(customer_scope(order), conversation, HIGH, summary())

        assert (handoff.status, handoff.priority) == (HandoffStatus.OPEN, HIGH)
        assert await conversation_status(engine, conversation) == "transferred"
        assert await audit_count(engine, order.sandbox_id, "approvals.handoff_created") == 1

    async def test_conversation_of_another_customer_conflicts(self, engine: AsyncEngine) -> None:
        order, [conversation] = await commit_order(engine)
        stranger = await commit_other_customer(engine, order)

        with pytest.raises(ConflictError) as raised:
            await handoffs_on(engine).create(stranger, conversation, HIGH, summary())

        assert raised.value.code == "handoff_conversation_mismatch"
        assert await conversation_status(engine, conversation) == "open"

    async def test_created_together_stores_one(self, engine: AsyncEngine) -> None:
        for _ in range(ROUNDS):
            order, [conversation] = await commit_order(engine)
            service = handoffs_on(engine)

            first, second = await asyncio.gather(
                service.create(customer_scope(order), conversation, HIGH, summary()),
                service.create(customer_scope(order), conversation, HIGH, summary()),
            )

            assert first.id == second.id
            assert await audit_count(engine, order.sandbox_id, "approvals.handoff_created") == 1

    async def test_two_reviewers_assigning_together_leave_one(self, engine: AsyncEngine) -> None:
        for _ in range(ROUNDS):
            order, [conversation] = await commit_order(engine)
            service = handoffs_on(engine)
            handoff = await service.create(customer_scope(order), conversation, HIGH, summary())

            results = await asyncio.gather(
                service.assign(reviewer_scope(order), handoff.id, ReviewerId(new_id())),
                service.assign(reviewer_scope(order), handoff.id, ReviewerId(new_id())),
                return_exceptions=True,
            )

            assert unexpected(results) == []
            assert conflicts(results) == ["handoff_already_assigned"]
            assert await audit_count(engine, order.sandbox_id, "approvals.handoff_assigned") == 1

    async def test_queue_puts_high_first_and_pages(self, engine: AsyncEngine) -> None:
        order, conversations = await commit_order(engine, conversations=5)
        priorities = (NORMAL, HIGH, NORMAL, HIGH, NORMAL)
        created: list[Handoff] = [
            await handoffs_on(engine, minute).create(
                customer_scope(order), conversation, priority, summary()
            )
            for minute, (conversation, priority) in enumerate(
                zip(conversations, priorities, strict=True)
            )
        ]
        service = handoffs_on(engine)
        scope = reviewer_scope(order)

        first = await service.queue(scope, PageRequest(limit=3))
        second = await service.queue(scope, PageRequest(limit=3, cursor=first.next_cursor))

        expected = [created[1], created[3], created[0], created[2], created[4]]
        assert list(first.items + second.items) == expected
        assert second.next_cursor is None

    async def test_other_sandbox_gets_not_found(self, engine: AsyncEngine) -> None:
        order, [conversation] = await commit_order(engine)
        service = handoffs_on(engine)
        handoff = await service.create(customer_scope(order), conversation, HIGH, summary())

        with pytest.raises(NotFoundError):
            await service.assign(
                SandboxScope(SandboxId(new_id())), handoff.id, ReviewerId(new_id())
            )
        page = await service.queue(SandboxScope(SandboxId(new_id())), PageRequest())
        assert page.items == ()

    async def test_queue_walks_the_partial_index(self, session: AsyncSession) -> None:
        order = delivered_order()
        await insert_orders(session, [order])
        store = PostgresHandoffStore(session)
        for minute in range(30):
            conversation = ConversationId(await insert_conversation(session, order))
            priority = Priority.HIGH if minute % 3 == 0 else Priority.NORMAL
            await store.create(
                customer_scope(order), handoff_at(order, conversation, priority, minute)
            )
        await session.execute(text("ANALYZE handoffs"))

        with StatementRecorder(session) as recorder:
            page = await store.queue(reviewer_scope(order), PageRequest(limit=10))

        assert len(page.items) == 10
        plan = await explain(session, recorder)
        assert "ix_handoffs_open_queue" in plan, plan
        assert "Seq Scan" not in plan, plan


def pending_at(order: Order, conversation: ConversationId, minute: int) -> ApprovalRequest:
    proposal = big_refund(order)
    return ApprovalRequest(
        id=ApprovalRequestId(new_id()),
        sandbox_id=order.sandbox_id,
        customer_id=order.customer_id,
        conversation_id=conversation,
        order_id=order.id,
        proposal_key=proposal_key(conversation, order.id, proposal),
        proposal=proposal,
        claimed_at=CLAIMED_AT,
        policy=needs_approval(),
        fraud=None,
        status=ApprovalStatus.PENDING,
        version=1,
        decided_by=None,
        decided_at=None,
        decision_reason=None,
        created_at=REQUESTED_AT + timedelta(minutes=minute),
    )


def handoff_at(
    order: Order, conversation: ConversationId, priority: Priority, minute: int
) -> Handoff:
    return Handoff(
        id=HandoffId(new_id()),
        sandbox_id=order.sandbox_id,
        customer_id=order.customer_id,
        conversation_id=conversation,
        priority=priority,
        summary=summary(),
        status=HandoffStatus.OPEN,
        assigned_to=None,
        assigned_at=None,
        created_at=REQUESTED_AT + timedelta(minutes=minute),
    )


async def explain(session: AsyncSession, recorder: StatementRecorder) -> str:
    """The plan of the one recorded query, with sequential scans off.

    Small test tables make a sequential scan look cheapest; turning it off asks whether a plan that
    walks the index exists at all.
    """
    [(statement, parameters)] = recorder.statements
    await session.execute(text("SET LOCAL enable_seqscan = off"))
    connection = await session.connection()
    result = await connection.exec_driver_sql(f"EXPLAIN {statement}", parameters)
    return "\n".join(row[0] for row in result)
