"""Builders of approval requests and handoffs for the approval tests."""

from datetime import timedelta

from support.action_builders import CLAIMED_AT, refund_of

from deliverius.approvals import (
    FraudAssessment,
    FraudFactor,
    HandoffSummary,
    NewApprovalRequest,
    Priority,
)
from deliverius.orders import Order
from deliverius.policy import ApprovalReason, NeedsApproval, PolicyDocumentId, ProposedAction
from deliverius.shared import ConversationId, CustomerScope, Money, SandboxScope

REQUESTED_AT = CLAIMED_AT + timedelta(seconds=5)
DECIDED_AT = REQUESTED_AT + timedelta(hours=2)
REASON = "Foto confere com o pedido"


def needs_approval(*reasons: ApprovalReason) -> NeedsApproval:
    return NeedsApproval(
        ceiling=Money(18_600),
        document=PolicyDocumentId.POL_02,
        reasons=reasons or (ApprovalReason.EXCEEDS_ACTION_LIMIT,),
    )


def big_refund(order: Order, cents: int = 18_600) -> ProposedAction:
    """A refund of the order's first item the policy sends to a person; `cents` tells two apart."""
    return refund_of(order.items[0], cents)


def new_request(
    order: Order,
    conversation_id: ConversationId,
    proposal: ProposedAction | None = None,
    *,
    fraud: FraudAssessment | None = None,
) -> NewApprovalRequest:
    return NewApprovalRequest(
        conversation_id=conversation_id,
        order_id=order.id,
        proposal=big_refund(order) if proposal is None else proposal,
        claimed_at=CLAIMED_AT,
        decision=needs_approval(),
        fraud=fraud,
    )


def fraud_assessment(score: float = 0.82) -> FraudAssessment:
    return FraudAssessment(
        score=score,
        model_version="fraud-v1",
        factors=(FraudFactor("refunds_last_30_days", 0.4), FraudFactor("account_age", -0.1)),
    )


def reviewer_scope(order: Order) -> SandboxScope:
    return SandboxScope(order.sandbox_id)


def customer_scope(order: Order) -> CustomerScope:
    return CustomerScope(order.sandbox_id, order.customer_id)


def summary(*facts: str, intent: str = "allergy") -> HandoffSummary:
    return HandoffSummary(intent=intent, facts=facts or ("cliente relata mal-estar",))


HIGH = Priority.HIGH
NORMAL = Priority.NORMAL
