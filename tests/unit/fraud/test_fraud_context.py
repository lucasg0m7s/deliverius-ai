"""`claim_context` turns the order and the proposal the agent has into what the features read."""

from dataclasses import replace
from datetime import timedelta

from support.fraud_builders import CLAIMED_AT, claim_history
from support.order_builders import build_order

from deliverius.fraud.context import claim_context
from deliverius.orders import DeliveryEvent, EventActor, Order, OrderStatus
from deliverius.policy import ActionKind, ClaimReason, ProposedAction
from deliverius.shared.ids import CustomerId, DeliveryEventId, SandboxId, new_id
from deliverius.shared.money import Money

HISTORY = claim_history()
PROPOSAL = ProposedAction(ActionKind.REFUND, ClaimReason.ITEM_MISSING, Money(1_500))


def an_order(*, delivered: bool) -> Order:
    return build_order(
        sandbox_id=SandboxId(new_id()),
        customer_id=CustomerId(new_id()),
        placed_at=CLAIMED_AT - timedelta(hours=3),
        delivered=delivered,
    )


def test_a_delivered_order_brings_its_delivery_instant() -> None:
    order = an_order(delivered=True)

    context = claim_context(order, PROPOSAL, CLAIMED_AT, HISTORY)

    delivered = next(e for e in order.events if e.status is OrderStatus.DELIVERED)
    assert context.delivered_at == delivered.occurred_at
    assert context.order_id == order.id
    assert context.order_total == order.total
    assert context.payment_method is order.payment_method
    assert context.requested == Money(1_500)
    assert (context.kind, context.reason) == (ActionKind.REFUND, ClaimReason.ITEM_MISSING)
    assert context.claimed_at == CLAIMED_AT
    assert context.history is HISTORY


def test_an_order_not_delivered_has_no_delivery_instant() -> None:
    context = claim_context(an_order(delivered=False), PROPOSAL, CLAIMED_AT, HISTORY)

    assert context.delivered_at is None


def test_a_cancelled_order_has_no_delivery_instant() -> None:
    placed = an_order(delivered=False)
    cancel = DeliveryEvent(
        DeliveryEventId(new_id()),
        OrderStatus.CANCELLED,
        EventActor.CUSTOMER,
        placed.events[-1].occurred_at + timedelta(minutes=1),
    )
    cancelled = replace(placed, status=OrderStatus.CANCELLED, events=(*placed.events, cancel))
    proposal = ProposedAction(
        ActionKind.CANCELLATION, ClaimReason.CUSTOMER_CANCELLATION, cancelled.total
    )

    context = claim_context(cancelled, proposal, CLAIMED_AT, HISTORY)

    assert context.delivered_at is None
    assert context.kind is ActionKind.CANCELLATION
