"""Builds the `ClaimContext` of a claim from what the agent already has: one code for both sides.

The application passes the order it read and the proposal the policy did not deny; the research
passes the same domain objects rebuilt from the dataset files.
"""

from datetime import datetime

from deliverius.fraud.models import ClaimContext, CustomerClaimHistory
from deliverius.orders import Order, OrderStatus
from deliverius.policy import ProposedAction


def claim_context(
    order: Order, proposal: ProposedAction, claimed_at: datetime, history: CustomerClaimHistory
) -> ClaimContext:
    return ClaimContext(
        order_id=order.id,
        claimed_at=claimed_at,
        kind=proposal.kind,
        reason=proposal.reason,
        payment_method=order.payment_method,
        order_total=order.total,
        requested=proposal.amount,
        delivered_at=_first_delivered_at(order),
        history=history,
    )


def _first_delivered_at(order: Order) -> datetime | None:
    """The delivery as the order's events tell it; the features decide if it came before."""
    delivered = [e.occurred_at for e in order.events if e.status is OrderStatus.DELIVERED]
    return min(delivered, default=None)
