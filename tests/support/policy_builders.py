"""Builders of proposals and customer histories for policy tests."""

from datetime import datetime, timedelta

from support.order_builders import BASE_TIME

from deliverius.orders import Order, OrderItem
from deliverius.policy import (
    ActionKind,
    ApprovedBy,
    ClaimedItem,
    ClaimReason,
    CustomerHistory,
    PriorAction,
    ProposedAction,
)
from deliverius.shared import Money, OrderId, new_id

# Delivered five minutes before the estimate (BASE_TIME + 45 min), so on time by default.
DELIVERED_AT = BASE_TIME + timedelta(minutes=40)


def claim(item: OrderItem, quantity: int | None = None) -> ClaimedItem:
    """Claims every unit of `item` unless told otherwise."""
    return ClaimedItem(item.id, item.quantity if quantity is None else quantity)


def propose(
    kind: ActionKind, reason: ClaimReason, cents: int, *items: ClaimedItem
) -> ProposedAction:
    return ProposedAction(kind=kind, reason=reason, amount=Money(cents), items=items)


def prior(
    kind: ActionKind,
    reason: ClaimReason,
    cents: int,
    *,
    order: Order | None = None,
    items: tuple[ClaimedItem, ...] = (),
    executed_at: datetime = DELIVERED_AT,
    approved_by: ApprovedBy = ApprovedBy.AUTOMATIC,
) -> PriorAction:
    """An executed action; on a fresh, unrelated order unless `order` is given."""
    return PriorAction(
        order_id=order.id if order is not None else OrderId(new_id()),
        kind=kind,
        reason=reason,
        amount=Money(cents),
        items=items,
        executed_at=executed_at,
        approved_by=approved_by,
    )


def history(*actions: PriorAction) -> CustomerHistory:
    return CustomerHistory(actions=actions)


def executed(
    action: ProposedAction, order: Order, at: datetime, approved_by: ApprovedBy
) -> PriorAction:
    """`action` as it enters the history once executed."""
    return PriorAction(
        order_id=order.id,
        kind=action.kind,
        reason=action.reason,
        amount=action.amount,
        items=action.items,
        executed_at=at,
        approved_by=approved_by,
    )
