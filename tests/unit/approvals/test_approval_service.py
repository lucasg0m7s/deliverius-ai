from dataclasses import replace
from datetime import timedelta

import pytest
from structlog.testing import capture_logs
from support.action_builders import delivered_order, new_conversation_id
from support.approval_builders import (
    DECIDED_AT,
    REASON,
    REQUESTED_AT,
    big_refund,
    customer_scope,
    fraud_assessment,
    new_request,
    reviewer_scope,
)

from deliverius.approvals import (
    MAX_REASON_LENGTH,
    ApprovalRequest,
    ApprovalService,
    ApprovalStatus,
    ConversationStatus,
    Decision,
    InMemoryApprovalUnitOfWork,
)
from deliverius.orders import Order, PageRequest
from deliverius.policy import proposal_key
from deliverius.shared import (
    ApprovalRequestId,
    AuditActorType,
    ConflictError,
    ConversationId,
    CustomerId,
    CustomerScope,
    FixedClock,
    InvalidInputError,
    NotFoundError,
    ReviewerId,
    SandboxId,
    SandboxScope,
    new_id,
)

REVIEWER = ReviewerId(new_id())
OTHER_REVIEWER = ReviewerId(new_id())


class World:
    """An order, its conversation and the service over the in-memory stores."""

    def __init__(self) -> None:
        self.order: Order = delivered_order()
        self.conversation: ConversationId = new_conversation_id()
        self.uow = InMemoryApprovalUnitOfWork()
        self.uow.conversations.add(self.conversation)
        self.clock = FixedClock(REQUESTED_AT)
        self.service = ApprovalService(self.uow, self.clock)

    @property
    def status(self) -> ConversationStatus:
        return self.uow.conversations.statuses[self.conversation]

    async def request(self, cents: int = 18_600) -> ApprovalRequest:
        return await self.service.request(
            customer_scope(self.order),
            new_request(self.order, self.conversation, big_refund(self.order, cents)),
        )

    async def decide(
        self,
        request: ApprovalRequest,
        decision: Decision = Decision.APPROVED,
        *,
        version: int = 1,
        reason: str = REASON,
        reviewer: ReviewerId = REVIEWER,
    ) -> ApprovalRequest:
        self.clock = FixedClock(DECIDED_AT)
        self.service = ApprovalService(self.uow, self.clock)
        return await self.service.decide(
            reviewer_scope(self.order),
            request.id,
            expected_version=version,
            decision=decision,
            reason=reason,
            reviewer_id=reviewer,
        )


@pytest.fixture
def world() -> World:
    return World()


class TestRequest:
    async def test_stores_a_pending_request_and_awaits_approval(self, world: World) -> None:
        with capture_logs() as logs:
            request = await world.request()

        proposal = big_refund(world.order)
        assert (request.status, request.version, request.created_at) == (
            ApprovalStatus.PENDING,
            1,
            REQUESTED_AT,
        )
        assert request.proposal_key == proposal_key(world.conversation, world.order.id, proposal)
        assert (request.sandbox_id, request.customer_id) == (
            world.order.sandbox_id,
            world.order.customer_id,
        )
        assert world.status is ConversationStatus.AWAITING_APPROVAL
        [entry] = world.uow.audit.entries
        assert (entry.action, entry.actor_type, entry.target_id) == (
            "approvals.requested",
            AuditActorType.AGENT,
            request.id,
        )
        assert entry.data == {
            "kind": "refund",
            "reason": "item_missing",
            "amount_cents": 18_600,
            "policy_reasons": ["exceeds_action_limit"],
        }
        assert [log["event"] for log in logs] == ["approvals.requested"]

    async def test_keeps_the_fraud_assessment(self, world: World) -> None:
        fraud = fraud_assessment()

        request = await world.service.request(
            customer_scope(world.order), new_request(world.order, world.conversation, fraud=fraud)
        )

        assert request.fraud == fraud

    async def test_same_proposal_again_returns_the_existing_request(self, world: World) -> None:
        first = await world.request()

        with capture_logs() as logs:
            second = await world.request()

        assert second == first
        assert len(world.uow.audit.entries) == 1
        [event] = logs
        assert (event["event"], event["status"]) == ("approvals.request_replayed", "pending")

    async def test_same_proposal_after_the_decision_returns_the_decided_one(
        self, world: World
    ) -> None:
        decided = await world.decide(await world.request())

        assert await world.request() == decided

    async def test_another_proposal_while_one_is_pending_conflicts(self, world: World) -> None:
        pending = await world.request()

        with capture_logs() as logs, pytest.raises(ConflictError) as raised:
            await world.request(cents=9_000)

        assert raised.value.code == "approval_already_pending"
        assert logs[0]["event"] == "approvals.pending_conflict"
        page = await world.service.pending_queue(reviewer_scope(world.order), PageRequest())
        assert page.items == (pending,)

    async def test_another_proposal_after_the_decision_is_allowed(self, world: World) -> None:
        await world.decide(await world.request(), Decision.REJECTED)

        second = await world.request(cents=9_000)

        assert second.status is ApprovalStatus.PENDING

    async def test_proposal_of_another_customer_conversation_conflicts(self, world: World) -> None:
        await world.request()
        stranger = CustomerScope(world.order.sandbox_id, CustomerId(new_id()))

        with capture_logs() as logs, pytest.raises(ConflictError) as raised:
            await world.service.request(stranger, new_request(world.order, world.conversation))

        assert raised.value.code == "approval_conversation_mismatch"
        assert logs[0]["event"] == "approvals.conversation_mismatch"


class TestGet:
    async def test_customer_and_reviewer_of_the_sandbox_read_it(self, world: World) -> None:
        request = await world.request()

        assert await world.service.get(customer_scope(world.order), request.id) == request
        assert await world.service.get(reviewer_scope(world.order), request.id) == request

    @pytest.mark.parametrize("stranger", ["customer", "sandbox"])
    async def test_another_scope_gets_not_found(self, world: World, stranger: str) -> None:
        request = await world.request()
        scope: CustomerScope | SandboxScope = (
            CustomerScope(world.order.sandbox_id, CustomerId(new_id()))
            if stranger == "customer"
            else SandboxScope(SandboxId(new_id()))
        )

        with pytest.raises(NotFoundError):
            await world.service.get(scope, request.id)


class TestDecide:
    async def test_approval_records_who_when_and_why(self, world: World) -> None:
        request = await world.request()

        with capture_logs() as logs:
            decided = await world.decide(request, reason=f"  {REASON}  ")

        assert (decided.status, decided.version) == (ApprovalStatus.APPROVED, 2)
        assert (decided.decided_by, decided.decided_at, decided.decision_reason) == (
            REVIEWER,
            DECIDED_AT,
            REASON,
        )
        assert world.status is ConversationStatus.OPEN
        entry = world.uow.audit.entries[-1]
        assert (entry.action, entry.actor_type, entry.actor_id) == (
            "approvals.decided",
            AuditActorType.REVIEWER,
            REVIEWER,
        )
        assert entry.data == {"decision": "approved", "version": 2}
        assert REASON not in str(logs)

    async def test_rejection_moves_to_rejected(self, world: World) -> None:
        decided = await world.decide(await world.request(), Decision.REJECTED)

        assert decided.status is ApprovalStatus.REJECTED
        assert world.uow.audit.entries[-1].data == {"decision": "rejected", "version": 2}

    async def test_same_decision_again_returns_the_state_without_auditing(
        self, world: World
    ) -> None:
        decided = await world.decide(await world.request())

        with capture_logs() as logs:
            again = await world.decide(decided, reviewer=OTHER_REVIEWER)

        assert again == decided
        assert len(world.uow.audit.entries) == 2
        assert logs[0]["event"] == "approvals.decision_replayed"

    async def test_opposite_decision_after_one_is_taken_conflicts(self, world: World) -> None:
        request = await world.request()
        await world.decide(request)

        with capture_logs() as logs, pytest.raises(ConflictError) as raised:
            await world.decide(request, Decision.REJECTED)

        assert raised.value.code == "approval_already_decided"
        assert (logs[0]["event"], logs[0]["approval_id"], logs[0]["version"]) == (
            "approvals.decision_conflict",
            str(request.id),
            2,
        )

    async def test_stale_version_on_a_pending_request_conflicts(self, world: World) -> None:
        request = await world.request()

        with capture_logs() as logs, pytest.raises(ConflictError) as raised:
            await world.decide(request, version=7)

        assert raised.value.code == "approval_version_stale"
        assert logs[0]["event"] == "approvals.version_stale"
        assert (await world.service.get(customer_scope(world.order), request.id)) == request

    async def test_request_of_another_sandbox_is_not_found(self, world: World) -> None:
        request = await world.request()

        with pytest.raises(NotFoundError):
            await world.service.decide(
                SandboxScope(SandboxId(new_id())),
                request.id,
                expected_version=1,
                decision=Decision.APPROVED,
                reason=REASON,
                reviewer_id=REVIEWER,
            )

    async def test_missing_request_is_not_found(self, world: World) -> None:
        request = await world.request()

        with pytest.raises(NotFoundError):
            await world.decide(replace(request, id=ApprovalRequestId(new_id())))

    @pytest.mark.parametrize(
        ("reason", "code"),
        [
            ("", "approval_decision.reason_empty"),
            ("   \n\t", "approval_decision.reason_empty"),
            ("x" * (MAX_REASON_LENGTH + 1), "approval_decision.reason_too_long"),
        ],
    )
    async def test_invalid_reason_leaves_the_request_pending(
        self, world: World, reason: str, code: str
    ) -> None:
        request = await world.request()

        with pytest.raises(InvalidInputError) as raised:
            await world.decide(request, reason=reason)

        assert str(raised.value) == code
        assert (await world.service.get(customer_scope(world.order), request.id)) == request
        assert world.status is ConversationStatus.AWAITING_APPROVAL

    async def test_reason_of_exactly_the_limit_is_accepted(self, world: World) -> None:
        reason = "x" * MAX_REASON_LENGTH

        decided = await world.decide(await world.request(), reason=reason)

        assert decided.decision_reason == reason


class TestPendingQueue:
    async def test_lists_only_pending_requests_of_the_sandbox(self, world: World) -> None:
        decided = await world.decide(await world.request())
        pending = await world.request(cents=9_000)
        other = World()
        await other.request()

        page = await world.service.pending_queue(reviewer_scope(world.order), PageRequest())

        assert page.items == (pending,)
        assert page.next_cursor is None
        assert decided not in page.items

    async def test_pages_of_fifty_then_ten_without_repeating_or_skipping(self) -> None:
        order = delivered_order()
        uow = InMemoryApprovalUnitOfWork()
        for minute in range(60):
            conversation = new_conversation_id()
            uow.conversations.add(conversation)
            service = ApprovalService(uow, FixedClock(REQUESTED_AT + timedelta(minutes=minute)))
            await service.request(customer_scope(order), new_request(order, conversation))
        scope = reviewer_scope(order)

        first = await service.pending_queue(scope, PageRequest())
        second = await service.pending_queue(scope, PageRequest(cursor=first.next_cursor))

        assert (len(first.items), len(second.items), second.next_cursor) == (50, 10, None)
        created = [request.created_at for request in first.items + second.items]
        assert created == [REQUESTED_AT + timedelta(minutes=minute) for minute in range(60)]
