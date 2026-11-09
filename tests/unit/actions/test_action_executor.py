from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from structlog.testing import capture_logs
from support.action_builders import (
    CLAIMED_AT,
    EXECUTED_AT,
    cancellation_of,
    delivered_order,
    delivered_order_of,
    executed_action,
    new_conversation_id,
    pizza,
    refund_of,
    request_for,
    reviewer_approval,
    scope_of,
)
from support.order_builders import build_item, build_order_in

from deliverius.actions import (
    ActionExecutor,
    Executed,
    ExecutedAction,
    InMemoryActionStore,
    InMemoryUnitOfWork,
    NotExecuted,
)
from deliverius.db.errors import DatabaseTimeoutError
from deliverius.orders import (
    DeliveryEvent,
    EventActor,
    InMemoryOrderRepository,
    Order,
    OrderStatus,
)
from deliverius.policy import (
    ActionKind,
    ApprovalReason,
    ApprovedBy,
    ClaimedItem,
    ClaimReason,
    DenialReason,
    Denied,
    NeedsApproval,
    ProposedAction,
    proposal_key,
)
from deliverius.shared import (
    ApprovalRequestId,
    AuditActorType,
    ConflictError,
    CustomerId,
    CustomerScope,
    FixedClock,
    InvariantError,
    Money,
    NotFoundError,
    OrderId,
    SandboxId,
    new_id,
)

SUCO = build_item("Suco", 800)
PIZZA = pizza()
CONVERSATION = new_conversation_id()


def delivered_pair() -> Order:
    return delivered_order(PIZZA, SUCO)


def executor_for(uow: InMemoryUnitOfWork, now: datetime = EXECUTED_AT) -> ActionExecutor:
    return ActionExecutor(uow, FixedClock(now))


async def test_allowed_refund_is_stored_as_automatic_with_one_audit_entry() -> None:
    order = delivered_pair()
    uow = InMemoryUnitOfWork([order])
    proposal = refund_of(SUCO, 800)

    result = await executor_for(uow).execute(
        scope_of(order), request_for(order, CONVERSATION, proposal)
    )

    assert isinstance(result, Executed)
    assert result.replayed is False
    action = result.action
    assert (action.proposal, action.approved_by, action.approval_request_id) == (
        proposal,
        ApprovedBy.AUTOMATIC,
        None,
    )
    assert action.idempotency_key == proposal_key(CONVERSATION, order.id, proposal)
    assert action.executed_at == EXECUTED_AT
    assert await uow.actions.list_for_order(scope_of(order), order.id) == (action,)
    [entry] = uow.audit.entries
    assert (entry.action, entry.actor_type, entry.target_id) == (
        "actions.executed",
        AuditActorType.AGENT,
        action.id,
    )
    assert entry.data == {
        "kind": "refund",
        "reason": "item_missing",
        "amount_cents": 800,
        "approved_by": "automatic",
    }


async def test_refund_needing_approval_is_not_executed_without_a_reviewer() -> None:
    order = delivered_pair()
    uow = InMemoryUnitOfWork([order])

    with capture_logs() as logs:
        result = await executor_for(uow).execute(
            scope_of(order), request_for(order, CONVERSATION, refund_of(PIZZA, 18_600))
        )

    assert isinstance(result, NotExecuted)
    assert isinstance(result.decision, NeedsApproval)
    assert result.decision.reasons == (
        ApprovalReason.EXCEEDS_ACTION_LIMIT,
        ApprovalReason.EXCEEDS_CUSTOMER_LIMIT,
    )
    assert await uow.actions.list_for_order(scope_of(order), order.id) == ()
    assert uow.audit.entries == []
    [event] = [log for log in logs if log["event"] == "actions.needs_approval"]
    assert event["reasons"] == ["exceeds_action_limit", "exceeds_customer_limit"]


async def test_refund_needing_approval_is_executed_with_a_reviewer_approval() -> None:
    order = delivered_pair()
    uow = InMemoryUnitOfWork([order])
    approval = reviewer_approval()

    result = await executor_for(uow).execute(
        scope_of(order), request_for(order, CONVERSATION, refund_of(PIZZA, 18_600), approval)
    )

    assert isinstance(result, Executed)
    assert result.action.approved_by is ApprovedBy.REVIEWER
    assert result.action.approval_request_id == approval.approval_request_id
    [entry] = uow.audit.entries
    assert entry.data == {
        "kind": "refund",
        "reason": "item_missing",
        "amount_cents": 18_600,
        "approved_by": "reviewer",
        "approval_request_id": str(approval.approval_request_id),
        "reviewer_id": str(approval.reviewer_id),
        "reevaluation_reasons": ["exceeds_action_limit", "exceeds_customer_limit"],
    }


async def test_allowed_refund_with_a_reviewer_approval_is_stored_as_reviewer() -> None:
    order = delivered_pair()
    uow = InMemoryUnitOfWork([order])
    approval = reviewer_approval()

    result = await executor_for(uow).execute(
        scope_of(order), request_for(order, CONVERSATION, refund_of(SUCO, 800), approval)
    )

    assert isinstance(result, Executed)
    assert result.action.approved_by is ApprovedBy.REVIEWER
    [entry] = uow.audit.entries
    assert "reevaluation_reasons" not in entry.data


async def test_denied_refund_is_not_executed_even_with_a_reviewer_approval() -> None:
    order = delivered_pair()
    uow = InMemoryUnitOfWork([order])
    too_much = refund_of(SUCO, 801)

    with capture_logs() as logs:
        result = await executor_for(uow).execute(
            scope_of(order), request_for(order, CONVERSATION, too_much, reviewer_approval())
        )

    assert isinstance(result, NotExecuted)
    assert isinstance(result.decision, Denied)
    assert result.decision.reason is DenialReason.EXCEEDS_CEILING
    assert await uow.actions.list_for_order(scope_of(order), order.id) == ()
    assert uow.audit.entries == []
    [event] = [log for log in logs if log["event"] == "actions.reevaluation_denied"]
    assert event["reason"] == "exceeds_ceiling"


async def test_same_proposal_executed_twice_returns_the_stored_action_without_new_audit() -> None:
    order = delivered_pair()
    uow = InMemoryUnitOfWork([order])
    executor = executor_for(uow)
    request = request_for(order, CONVERSATION, refund_of(SUCO, 800))

    first = await executor.execute(scope_of(order), request)
    with capture_logs() as logs:
        second = await executor.execute(scope_of(order), request)

    assert isinstance(first, Executed)
    assert isinstance(second, Executed)
    assert (first.replayed, second.replayed) == (False, True)
    assert second.action == first.action
    assert len(uow.audit.entries) == 1
    assert len(await uow.actions.list_for_order(scope_of(order), order.id)) == 1
    assert [log["event"] for log in logs] == ["actions.replayed"]


async def test_same_proposal_with_items_in_another_order_is_a_replay() -> None:
    order = delivered_pair()
    uow = InMemoryUnitOfWork([order])
    executor = executor_for(uow)
    items = (ClaimedItem(PIZZA.id, 1), ClaimedItem(SUCO.id, 1))
    proposal = ProposedAction(ActionKind.REFUND, ClaimReason.ITEM_MISSING, Money(800), items)
    reordered = replace(proposal, items=tuple(reversed(items)))

    first = await executor.execute(scope_of(order), request_for(order, CONVERSATION, proposal))
    second = await executor.execute(scope_of(order), request_for(order, CONVERSATION, reordered))

    assert isinstance(first, Executed)
    assert isinstance(second, Executed)
    assert second.replayed is True
    assert second.action.id == first.action.id


class BlindOnceStore(InMemoryActionStore):
    """Does not see the stored action on the first lookup: the race the unique key backs up."""

    def __init__(self, inner: InMemoryActionStore) -> None:
        super().__init__()
        self._inner = inner
        self._blind = True

    async def find_by_key(
        self, scope: CustomerScope, idempotency_key: str
    ) -> ExecutedAction | None:
        if self._blind:
            self._blind = False
            return None
        return await self._inner.find_by_key(scope, idempotency_key)

    async def insert(self, action: ExecutedAction) -> ExecutedAction:
        return await self._inner.insert(action)


async def test_replay_found_only_at_insert_time_returns_the_stored_action_without_audit() -> None:
    order = delivered_pair()
    uow = InMemoryUnitOfWork([order])
    proposal = refund_of(SUCO, 800)
    stored = executed_action(order, CONVERSATION, proposal)
    await uow.actions.insert(stored)
    uow.actions = BlindOnceStore(uow.actions)

    result = await executor_for(uow).execute(
        scope_of(order), request_for(order, CONVERSATION, proposal)
    )

    assert result == Executed(stored, replayed=True)
    assert uow.audit.entries == []


async def test_cancellation_stores_the_total_and_cancels_the_order() -> None:
    order = build_order_in(OrderStatus.PLACED, (PIZZA,))
    uow = InMemoryUnitOfWork([order])

    result = await executor_for(uow).execute(
        scope_of(order), request_for(order, CONVERSATION, cancellation_of(order))
    )

    assert isinstance(result, Executed)
    assert result.action.proposal.amount == order.total
    cancelled = await uow.orders.lock_order(scope_of(order), order.id)
    assert cancelled is not None
    assert cancelled.status is OrderStatus.CANCELLED
    event = cancelled.events[-1]
    assert event == DeliveryEvent(event.id, OrderStatus.CANCELLED, EventActor.SYSTEM, EXECUTED_AT)
    assert len(uow.audit.entries) == 1


async def test_cancellation_of_an_order_out_for_delivery_is_denied_and_changes_nothing() -> None:
    order = build_order_in(OrderStatus.OUT_FOR_DELIVERY, (PIZZA,))
    uow = InMemoryUnitOfWork([order])

    result = await executor_for(uow).execute(
        scope_of(order),
        request_for(order, CONVERSATION, cancellation_of(order), reviewer_approval()),
    )

    assert isinstance(result, NotExecuted)
    assert isinstance(result.decision, Denied)
    assert result.decision.reason is DenialReason.ORDER_ALREADY_DISPATCHED
    assert await uow.orders.lock_order(scope_of(order), order.id) == order
    assert uow.audit.entries == []


async def test_coupon_does_not_cancel_the_order() -> None:
    order = build_order_in(OrderStatus.DELIVERED, (PIZZA,), final_event_at=CLAIMED_AT)
    uow = InMemoryUnitOfWork([order])
    coupon = ProposedAction(
        ActionKind.COUPON, ClaimReason.QUALITY_ISSUE, Money(1_000), (ClaimedItem(PIZZA.id, 1),)
    )

    result = await executor_for(uow).execute(
        scope_of(order), request_for(order, CONVERSATION, coupon)
    )

    assert isinstance(result, Executed)
    assert await uow.orders.lock_order(scope_of(order), order.id) == order


class TimingOutOrders:
    """The in-memory orders, except that the lock the test names never comes."""

    def __init__(self, inner: InMemoryOrderRepository, failing: str) -> None:
        self._inner = inner
        self._failing = failing

    async def lock_customer(self, scope: CustomerScope) -> CustomerId | None:
        if self._failing == "customer":
            raise DatabaseTimeoutError
        return await self._inner.lock_customer(scope)

    async def lock_order(self, scope: CustomerScope, order_id: OrderId) -> Order | None:
        if self._failing == "order":
            raise DatabaseTimeoutError
        return await self._inner.lock_order(scope, order_id)

    async def mark_cancelled(self, scope: CustomerScope, order_id: OrderId, at: datetime) -> None:
        await self._inner.mark_cancelled(scope, order_id, at)


@pytest.mark.parametrize("failing", ["customer", "order"])
async def test_lock_that_times_out_is_a_retryable_conflict_and_nothing_is_written(
    failing: str,
) -> None:
    order = delivered_pair()
    uow = InMemoryUnitOfWork([order])
    uow.orders = TimingOutOrders(uow.orders, failing)

    with capture_logs() as logs, pytest.raises(ConflictError) as error:
        await executor_for(uow).execute(
            scope_of(order), request_for(order, CONVERSATION, refund_of(SUCO, 800))
        )

    assert error.value.code == "action_lock_timeout"
    assert [log["event"] for log in logs] == ["actions.lock_conflict"]
    assert uow.audit.entries == []


class RefusingStore(InMemoryActionStore):
    """Stands for the database refusing the row on a foreign key."""

    def __init__(self, code: str) -> None:
        super().__init__()
        self._code = code

    async def insert(self, action: ExecutedAction) -> ExecutedAction:
        raise ConflictError(self._code)


@pytest.mark.parametrize(
    ("code", "event"),
    [
        ("action_approval_mismatch", "actions.approval_mismatch"),
        ("action_conversation_mismatch", "actions.conversation_mismatch"),
    ],
)
async def test_insert_refused_by_the_database_is_logged_and_raised_and_leaves_no_audit(
    code: str, event: str
) -> None:
    order = delivered_pair()
    uow = InMemoryUnitOfWork([order])
    uow.actions = RefusingStore(code)

    with capture_logs() as logs, pytest.raises(ConflictError) as error:
        await executor_for(uow).execute(
            scope_of(order),
            request_for(order, CONVERSATION, refund_of(PIZZA, 18_600), reviewer_approval()),
        )

    assert error.value.code == code
    assert [log["event"] for log in logs] == [event]
    assert uow.audit.entries == []


async def test_deadline_counts_from_the_claim_not_from_the_execution() -> None:
    order = delivered_pair()
    assert order.delivered_at is not None
    claimed_at = order.delivered_at + timedelta(hours=23)
    uow = InMemoryUnitOfWork([order])
    request = request_for(
        order, CONVERSATION, refund_of(PIZZA, 18_600), reviewer_approval(), claimed_at=claimed_at
    )

    result = await executor_for(uow, now=claimed_at + timedelta(hours=2)).execute(
        scope_of(order), request
    )

    assert isinstance(result, Executed)
    assert result.action.claimed_at == claimed_at
    assert result.action.executed_at == claimed_at + timedelta(hours=2)


async def test_claim_made_after_the_deadline_is_denied() -> None:
    order = delivered_pair()
    assert order.delivered_at is not None
    claimed_at = order.delivered_at + timedelta(hours=25)
    uow = InMemoryUnitOfWork([order])

    result = await executor_for(uow, now=claimed_at).execute(
        scope_of(order),
        request_for(order, CONVERSATION, refund_of(SUCO, 800), claimed_at=claimed_at),
    )

    assert isinstance(result, NotExecuted)
    assert isinstance(result.decision, Denied)
    assert result.decision.reason is DenialReason.CLAIM_WINDOW_EXPIRED


async def test_actions_on_other_orders_of_the_customer_count_toward_the_limit() -> None:
    first = delivered_order_of(delivered_pair(), 9_000)
    second = delivered_order_of(first, 9_000)
    third = delivered_order_of(first, 9_000)
    uow = InMemoryUnitOfWork([first, second, third])
    executor = executor_for(uow)

    results = [
        await executor.execute(
            scope_of(order), request_for(order, CONVERSATION, refund_of(order.items[0], cents))
        )
        for order, cents in ((first, 5_000), (second, 5_000), (third, 100))
    ]

    assert [type(result) for result in results] == [Executed, Executed, NotExecuted]
    refused = results[2]
    assert isinstance(refused, NotExecuted)
    assert isinstance(refused.decision, NeedsApproval)
    assert refused.decision.reasons == (ApprovalReason.EXCEEDS_CUSTOMER_LIMIT,)


async def test_order_of_another_customer_is_not_found_and_nothing_is_written() -> None:
    order = delivered_pair()
    uow = InMemoryUnitOfWork([order])
    neighbour = CustomerScope(order.sandbox_id, CustomerId(new_id()))

    with pytest.raises(NotFoundError):
        await executor_for(uow).execute(
            neighbour, request_for(order, CONVERSATION, refund_of(SUCO, 800))
        )

    assert uow.audit.entries == []
    assert await uow.actions.list_for_order(scope_of(order), order.id) == ()


async def test_unknown_customer_is_not_found() -> None:
    order = delivered_pair()
    uow = InMemoryUnitOfWork([order])
    stranger = CustomerScope(SandboxId(new_id()), CustomerId(new_id()))

    with pytest.raises(NotFoundError):
        await executor_for(uow).execute(
            stranger, request_for(order, CONVERSATION, refund_of(SUCO, 800))
        )


async def test_unknown_order_of_a_known_customer_is_not_found() -> None:
    order = delivered_pair()
    other = replace(
        delivered_order(PIZZA), customer_id=order.customer_id, sandbox_id=order.sandbox_id
    )
    uow = InMemoryUnitOfWork([order])

    with pytest.raises(NotFoundError):
        await executor_for(uow).execute(
            scope_of(order), request_for(other, CONVERSATION, refund_of(PIZZA, 100))
        )


def test_request_with_a_naive_claim_instant_is_an_invariant_error() -> None:
    with pytest.raises(InvariantError) as error:
        request_for(
            delivered_pair(),
            CONVERSATION,
            refund_of(SUCO, 800),
            claimed_at=CLAIMED_AT.replace(tzinfo=None),
        )

    assert error.value.rule == "naive_datetime.claimed_at"


@pytest.mark.parametrize(
    "inconsistency",
    [
        {"approved_by": ApprovedBy.REVIEWER},
        {"approval_request_id": ApprovalRequestId(new_id())},
    ],
    ids=["reviewer-without-approval", "approval-without-reviewer"],
)
def test_executed_action_has_an_approval_exactly_when_a_reviewer_approved(
    inconsistency: dict[str, object],
) -> None:
    automatic = executed_action(delivered_pair(), CONVERSATION, refund_of(SUCO, 800))

    with pytest.raises(InvariantError) as error:
        replace(automatic, **inconsistency)

    assert error.value.rule == "executed_action.approval_inconsistent"
