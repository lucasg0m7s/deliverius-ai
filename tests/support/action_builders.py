"""Builders of orders, requests and executed actions for the action tests."""

from dataclasses import replace
from datetime import datetime, timedelta

from support.order_builders import build_item, build_order_in
from support.policy_builders import DELIVERED_AT

from deliverius.actions import Automatic, ExecutedAction, ExecutionRequest, ReviewerApproved
from deliverius.actions.models import Authority
from deliverius.orders import Order, OrderItem, OrderStatus
from deliverius.policy import (
    ActionKind,
    ApprovedBy,
    ClaimedItem,
    ClaimReason,
    ProposedAction,
    proposal_key,
)
from deliverius.shared import (
    ActionId,
    ApprovalRequestId,
    ConversationId,
    CustomerScope,
    Money,
    ReviewerId,
    new_id,
)

# Twenty minutes after the delivery: inside every claim window and under every customer limit.
CLAIMED_AT = DELIVERED_AT + timedelta(minutes=20)
EXECUTED_AT = CLAIMED_AT + timedelta(seconds=5)


def pizza() -> OrderItem:
    return build_item("Pizza", 18_600)


def delivered_order(*items: OrderItem) -> Order:
    return build_order_in(OrderStatus.DELIVERED, items or (pizza(),), final_event_at=DELIVERED_AT)


def delivered_order_of(customer: Order, item_cents: int) -> Order:
    """Another delivered order of the same customer, with one item."""
    order = build_order_in(
        OrderStatus.DELIVERED,
        (build_item("Combo", item_cents),),
        customer_id=customer.customer_id,
        final_event_at=customer.delivered_at,
    )
    return replace(order, sandbox_id=customer.sandbox_id)


def scope_of(order: Order) -> CustomerScope:
    return CustomerScope(order.sandbox_id, order.customer_id)


def new_conversation_id() -> ConversationId:
    return ConversationId(new_id())


def refund_of(item: OrderItem, cents: int, quantity: int | None = None) -> ProposedAction:
    return ProposedAction(
        kind=ActionKind.REFUND,
        reason=ClaimReason.ITEM_MISSING,
        amount=Money(cents),
        items=(ClaimedItem(item.id, item.quantity if quantity is None else quantity),),
    )


def cancellation_of(order: Order) -> ProposedAction:
    return ProposedAction(
        kind=ActionKind.CANCELLATION,
        reason=ClaimReason.CUSTOMER_CANCELLATION,
        amount=order.total,
    )


def reviewer_approval() -> ReviewerApproved:
    return ReviewerApproved(ApprovalRequestId(new_id()), ReviewerId(new_id()))


def request_for(
    order: Order,
    conversation_id: ConversationId,
    proposal: ProposedAction,
    authority: Authority | None = None,
    *,
    claimed_at: datetime = CLAIMED_AT,
) -> ExecutionRequest:
    return ExecutionRequest(
        conversation_id=conversation_id,
        order_id=order.id,
        proposal=proposal,
        claimed_at=claimed_at,
        authority=Automatic() if authority is None else authority,
    )


def executed_action(
    order: Order,
    conversation_id: ConversationId,
    proposal: ProposedAction,
    *,
    approval: ReviewerApproved | None = None,
    key: str | None = None,
    executed_at: datetime = EXECUTED_AT,
) -> ExecutedAction:
    return ExecutedAction(
        id=ActionId(new_id()),
        sandbox_id=order.sandbox_id,
        customer_id=order.customer_id,
        order_id=order.id,
        conversation_id=conversation_id,
        proposal=proposal,
        idempotency_key=key or proposal_key(conversation_id, order.id, proposal),
        approved_by=ApprovedBy.AUTOMATIC if approval is None else ApprovedBy.REVIEWER,
        approval_request_id=None if approval is None else approval.approval_request_id,
        claimed_at=CLAIMED_AT,
        executed_at=executed_at,
    )
