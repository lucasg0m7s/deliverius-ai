"""Hypothesis strategies for orders, proposals and customer histories."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from hypothesis import strategies as st
from support.order_builders import BASE_TIME, build_item, build_order_in
from support.policy_builders import prior

from deliverius.orders import Order, OrderItem, OrderStatus
from deliverius.policy import (
    ActionKind,
    ApprovedBy,
    ClaimedItem,
    ClaimReason,
    CustomerHistory,
    PriorAction,
    ProposedAction,
)
from deliverius.shared import Money, OrderItemId, new_id

ITEM_REASONS = (ClaimReason.ITEM_MISSING, ClaimReason.WRONG_ITEM, ClaimReason.QUALITY_ISSUE)
# Test data from the spec, used only to steer examples toward decisions past the shape checks.
COVERED_KINDS = {
    ClaimReason.ITEM_MISSING: (ActionKind.REFUND,),
    ClaimReason.WRONG_ITEM: (ActionKind.REFUND,),
    ClaimReason.QUALITY_ISSUE: (ActionKind.REFUND, ActionKind.COUPON),
    ClaimReason.LATE_DELIVERY: (ActionKind.COUPON, ActionKind.CANCELLATION),
    ClaimReason.NOT_DELIVERED: (ActionKind.REFUND,),
    ClaimReason.DUPLICATE_CHARGE: (ActionKind.REFUND,),
    ClaimReason.CUSTOMER_CANCELLATION: (ActionKind.CANCELLATION,),
}


@dataclass(frozen=True, slots=True)
class Scenario:
    action: ProposedAction
    order: Order
    history: CustomerHistory
    claimed_at: datetime


items = st.builds(build_item, st.just("Item"), st.integers(100, 9_000), st.integers(1, 3))


@st.composite
def orders(draw: st.DrawFn) -> Order:
    order_items = tuple(draw(st.lists(items, min_size=1, max_size=3)))
    fee = Money(draw(st.integers(0, 1_000)))
    subtotal = Money.sum(item.total for item in order_items)
    return build_order_in(
        draw(st.one_of(st.just(OrderStatus.DELIVERED), st.sampled_from(OrderStatus))),
        order_items,
        final_event_at=BASE_TIME + timedelta(minutes=draw(st.integers(10, 240))),
        delivery_fee=fee,
        discount=Money(draw(st.integers(0, (subtotal + fee).cents))),
    )


def claimed_items(order: Order, *, full: bool) -> st.SearchStrategy[tuple[ClaimedItem, ...]]:
    """Units of the order's items: every unit when `full`, else any quantity or a foreign item."""
    if full:
        return st.lists(st.sampled_from(order.items), min_size=1, unique=True).map(
            lambda chosen: tuple(ClaimedItem(item.id, item.quantity) for item in chosen)
        )
    known = st.sampled_from([item.id for item in order.items])
    foreign = st.builds(lambda: OrderItemId(new_id()))
    claimed = st.builds(ClaimedItem, st.one_of(known, foreign), st.integers(-1, 4))
    return st.lists(claimed, max_size=3).map(tuple)


@st.composite
def proposals(draw: st.DrawFn, order: Order, *, plausible: bool = False) -> ProposedAction:
    """Anything the LLM might send; `plausible` keeps to covered, well-formed proposals."""
    reason = draw(st.sampled_from(ClaimReason))
    covered_kind = st.sampled_from(COVERED_KINDS[reason])
    full_items = claimed_items(order, full=True)
    if plausible:
        kind = draw(covered_kind)
        claimed = draw(full_items) if reason in ITEM_REASONS else ()
        amount = draw(st.integers(1, 6_000))
    else:
        kind = draw(st.one_of(covered_kind, st.sampled_from(ActionKind)))
        claimed = draw(st.one_of(full_items, claimed_items(order, full=False), st.just(())))
        amount = draw(st.integers(0, 30_000))
    return ProposedAction(kind=kind, reason=reason, amount=Money(amount), items=claimed)


@st.composite
def prior_actions(draw: st.DrawFn, order: Order, claimed_at: datetime) -> PriorAction:
    on_this_order = draw(st.booleans())
    chosen: OrderItem = draw(st.sampled_from(order.items))
    with_item = on_this_order and draw(st.booleans())
    return prior(
        draw(st.sampled_from(ActionKind)),
        draw(st.sampled_from(ClaimReason)),
        draw(st.integers(1, 8_000)),
        order=order if on_this_order else None,
        items=(ClaimedItem(chosen.id, draw(st.integers(1, chosen.quantity))),) if with_item else (),
        executed_at=claimed_at - timedelta(minutes=draw(st.integers(0, 60 * 24 * 40))),
        approved_by=draw(st.sampled_from(ApprovedBy)),
    )


def claim_instants(order: Order) -> st.SearchStrategy[datetime]:
    return st.integers(0, 60 * 24 * 3).map(
        lambda minutes: order.placed_at + timedelta(minutes=minutes)
    )


@st.composite
def scenarios(draw: st.DrawFn, *, plausible: bool = False) -> Scenario:
    order = draw(orders())
    claimed_at = draw(claim_instants(order))
    history = CustomerHistory(
        tuple(draw(st.lists(prior_actions(order, claimed_at), max_size=0 if plausible else 4)))
    )
    return Scenario(draw(proposals(order, plausible=plausible)), order, history, claimed_at)
