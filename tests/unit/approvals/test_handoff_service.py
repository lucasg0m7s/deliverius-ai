from datetime import timedelta

import pytest
from structlog.testing import capture_logs
from support.action_builders import delivered_order, new_conversation_id
from support.approval_builders import (
    HIGH,
    NORMAL,
    REQUESTED_AT,
    customer_scope,
    reviewer_scope,
    summary,
)

from deliverius.approvals import (
    ConversationStatus,
    Handoff,
    HandoffService,
    HandoffStatus,
    HandoffSummary,
    InMemoryApprovalUnitOfWork,
    Priority,
)
from deliverius.orders import Order, PageRequest
from deliverius.shared import (
    AuditActorType,
    ConflictError,
    ConversationId,
    CustomerId,
    CustomerScope,
    FixedClock,
    HandoffId,
    InvalidInputError,
    NotFoundError,
    ReviewerId,
    SandboxId,
    SandboxScope,
    new_id,
)

REVIEWER = ReviewerId(new_id())
CPF = "529.982.247-25"


class World:
    def __init__(self) -> None:
        self.order: Order = delivered_order()
        self.conversation: ConversationId = new_conversation_id()
        self.uow = InMemoryApprovalUnitOfWork()
        self.uow.conversations.add(self.conversation)
        self.service = HandoffService(self.uow, FixedClock(REQUESTED_AT))

    async def create(
        self,
        priority: Priority = HIGH,
        handoff_summary: HandoffSummary | None = None,
        conversation: ConversationId | None = None,
    ) -> Handoff:
        return await self.service.create(
            customer_scope(self.order),
            conversation or self.conversation,
            priority,
            handoff_summary or summary(),
        )

    async def assign(self, handoff: Handoff, reviewer: ReviewerId = REVIEWER) -> Handoff:
        return await self.service.assign(reviewer_scope(self.order), handoff.id, reviewer)


@pytest.fixture
def world() -> World:
    return World()


class TestCreate:
    async def test_stores_an_open_handoff_and_transfers_the_conversation(
        self, world: World
    ) -> None:
        with capture_logs() as logs:
            handoff = await world.create()

        assert (handoff.status, handoff.priority, handoff.summary) == (
            HandoffStatus.OPEN,
            HIGH,
            summary(),
        )
        assert world.uow.conversations.statuses[world.conversation] is (
            ConversationStatus.TRANSFERRED
        )
        [entry] = world.uow.audit.entries
        assert (entry.action, entry.actor_type, entry.data) == (
            "approvals.handoff_created",
            AuditActorType.AGENT,
            {"priority": "high", "intent": "allergy"},
        )
        assert [log["event"] for log in logs] == ["approvals.handoff_created"]

    async def test_repeat_returns_the_open_handoff(self, world: World) -> None:
        first = await world.create()

        with capture_logs() as logs:
            second = await world.create(NORMAL, summary("outro texto"))

        assert second == first
        assert len(world.uow.audit.entries) == 1
        assert logs[0]["event"] == "approvals.handoff_replayed"

    async def test_open_handoff_of_another_customer_conflicts(self, world: World) -> None:
        await world.create()
        stranger = CustomerScope(world.order.sandbox_id, CustomerId(new_id()))

        with capture_logs() as logs, pytest.raises(ConflictError) as raised:
            await world.service.create(stranger, world.conversation, HIGH, summary())

        assert raised.value.code == "handoff_conversation_mismatch"
        assert logs[0]["event"] == "approvals.handoff_conversation_mismatch"

    async def test_closed_conversation_is_not_reopened(self, world: World) -> None:
        world.uow.conversations.statuses[world.conversation] = ConversationStatus.CLOSED

        await world.create()

        assert world.uow.conversations.statuses[world.conversation] is ConversationStatus.CLOSED

    @pytest.mark.parametrize(
        ("handoff_summary", "field"),
        [
            (summary(f"cliente informou o CPF {CPF}"), "facts[0]"),
            (summary("ok", "ligar para (11) 98765-4321"), "facts[1]"),
            (
                HandoffSummary(intent="allergy", actions_taken=("e-mail para ana@exemplo.com",)),
                "actions_taken[0]",
            ),
        ],
    )
    async def test_summary_with_personal_data_is_refused_and_nothing_stored(
        self, world: World, handoff_summary: HandoffSummary, field: str
    ) -> None:
        with capture_logs() as logs, pytest.raises(InvalidInputError) as raised:
            await world.create(handoff_summary=handoff_summary)

        assert str(raised.value) == "handoff_summary.personal_data"
        assert (logs[0]["event"], logs[0]["field"]) == (
            "approvals.handoff_rejected_personal_data",
            field,
        )
        assert CPF not in str(logs)
        assert world.uow.audit.entries == []
        assert world.uow.conversations.statuses[world.conversation] is ConversationStatus.OPEN

    async def test_masking_markers_are_accepted(self, world: World) -> None:
        handoff = await world.create(handoff_summary=summary("cliente informou o [CPF] e [EMAIL]"))

        assert handoff.summary.facts == ("cliente informou o [CPF] e [EMAIL]",)


class TestQueue:
    async def test_high_priority_first_then_oldest(self, world: World) -> None:
        normal = await world.create(NORMAL, conversation=_open(world))
        world.service = HandoffService(world.uow, FixedClock(REQUESTED_AT + timedelta(hours=1)))
        late_high = await world.create(HIGH, conversation=_open(world))
        world.service = HandoffService(world.uow, FixedClock(REQUESTED_AT - timedelta(hours=1)))
        high = await world.create(HIGH, conversation=_open(world))

        page = await world.service.queue(reviewer_scope(world.order), PageRequest())

        assert page.items == (high, late_high, normal)

    async def test_pages_follow_the_order_without_repeating(self, world: World) -> None:
        created = [
            await world.create(priority, conversation=_open(world))
            for priority in (NORMAL, HIGH, NORMAL, HIGH, NORMAL)
        ]
        scope = reviewer_scope(world.order)

        first = await world.service.queue(scope, PageRequest(limit=2))
        assert first.next_cursor is not None
        second = await world.service.queue(scope, PageRequest(limit=2, cursor=first.next_cursor))
        assert second.next_cursor is not None
        third = await world.service.queue(scope, PageRequest(limit=2, cursor=second.next_cursor))

        seen = first.items + second.items + third.items
        assert third.next_cursor is None
        assert [handoff.priority for handoff in seen] == [HIGH, HIGH, NORMAL, NORMAL, NORMAL]
        assert sorted(handoff.id for handoff in seen) == sorted(h.id for h in created)

    async def test_assigned_and_other_sandbox_handoffs_are_left_out(self, world: World) -> None:
        await world.assign(await world.create())
        other = World()
        await other.create()

        page = await world.service.queue(reviewer_scope(world.order), PageRequest())

        assert page.items == ()

    async def test_tampered_cursor_is_invalid_input(self, world: World) -> None:
        with pytest.raises(InvalidInputError):
            await world.service.queue(reviewer_scope(world.order), PageRequest(cursor="bm9wZQ"))


class TestAssign:
    async def test_records_the_reviewer_and_audits(self, world: World) -> None:
        handoff = await world.create()

        with capture_logs() as logs:
            assigned = await world.assign(handoff)

        assert (assigned.status, assigned.assigned_to, assigned.assigned_at) == (
            HandoffStatus.ASSIGNED,
            REVIEWER,
            REQUESTED_AT,
        )
        entry = world.uow.audit.entries[-1]
        assert (entry.action, entry.actor_type, entry.actor_id) == (
            "approvals.handoff_assigned",
            AuditActorType.REVIEWER,
            REVIEWER,
        )
        assert logs[0]["event"] == "approvals.handoff_assigned"

    async def test_same_reviewer_again_gets_it_back_without_auditing(self, world: World) -> None:
        assigned = await world.assign(await world.create())

        again = await world.assign(assigned)

        assert again == assigned
        assert len(world.uow.audit.entries) == 2

    async def test_another_reviewer_conflicts(self, world: World) -> None:
        handoff = await world.create()
        await world.assign(handoff)

        with capture_logs() as logs, pytest.raises(ConflictError) as raised:
            await world.assign(handoff, ReviewerId(new_id()))

        assert raised.value.code == "handoff_already_assigned"
        assert logs[0]["event"] == "approvals.handoff_assign_conflict"

    async def test_missing_or_other_sandbox_is_not_found(self, world: World) -> None:
        handoff = await world.create()

        with pytest.raises(NotFoundError):
            await world.service.assign(SandboxScope(SandboxId(new_id())), handoff.id, REVIEWER)
        with pytest.raises(NotFoundError):
            await world.service.assign(reviewer_scope(world.order), HandoffId(new_id()), REVIEWER)


def _open(world: World) -> ConversationId:
    conversation = new_conversation_id()
    world.uow.conversations.add(conversation)
    return conversation
