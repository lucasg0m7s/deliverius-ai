"""Hypothesis strategies: a customer with a few orders and a sequence of execution attempts."""

from dataclasses import dataclass, replace
from datetime import timedelta

from hypothesis import strategies as st
from support.action_builders import CLAIMED_AT
from support.order_builders import build_item, build_order_in
from support.policy_builders import DELIVERED_AT

from deliverius.orders import Order, OrderStatus
from deliverius.policy import ActionKind, ClaimedItem, ClaimReason, ProposedAction
from deliverius.shared import Money, OrderItemId, new_id

ITEM_REASONS = (ClaimReason.ITEM_MISSING, ClaimReason.WRONG_ITEM, ClaimReason.QUALITY_ISSUE)
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
class Attempt:
    order_index: int
    proposal: ProposedAction
    reviewer_approved: bool
    minutes_later: int


items = st.builds(build_item, st.just("Item"), st.integers(500, 9_000), st.integers(1, 2))


@st.composite
def customer_orders(draw: st.DrawFn) -> tuple[Order, ...]:
    """Delivered or just-placed orders of one customer in one sandbox."""
    count = draw(st.integers(2, 3))
    first: Order | None = None
    orders: list[Order] = []
    for _ in range(count):
        status = draw(st.sampled_from([OrderStatus.DELIVERED] * 3 + [OrderStatus.PLACED]))
        order = build_order_in(
            status,
            tuple(draw(st.lists(items, min_size=1, max_size=4))),
            final_event_at=DELIVERED_AT,
            delivery_fee=Money(draw(st.integers(0, 1_000))),
            customer_id=None if first is None else first.customer_id,
        )
        if first is None:
            first = order
        orders.append(replace(order, sandbox_id=first.sandbox_id))
    return tuple(orders)


def claimed_items(order: Order) -> st.SearchStrategy[tuple[ClaimedItem, ...]]:
    known = st.sampled_from([item.id for item in order.items])
    foreign = st.builds(lambda: OrderItemId(new_id()))
    claimed = st.builds(ClaimedItem, st.one_of(known, foreign), st.integers(-1, 4))
    return st.lists(claimed, max_size=3).map(tuple)


def single_full_claim(order: Order) -> st.SearchStrategy[tuple[ClaimedItem, ...]]:
    return st.sampled_from(order.items).map(lambda item: (ClaimedItem(item.id, item.quantity),))


@st.composite
def proposals(draw: st.DrawFn, order: Order, *, plausible: bool) -> ProposedAction:
    """`plausible` keeps to covered, well-formed proposals so many of them get executed."""
    # Item reasons weigh more: they are the ones the policy can allow on its own.
    reason = draw(st.sampled_from([*ITEM_REASONS] * 3 + [*ClaimReason]))
    covered = st.sampled_from(COVERED_KINDS[reason])
    if plausible:
        kind = draw(covered)
        claimed = draw(single_full_claim(order)) if reason in ITEM_REASONS else ()
        amount = draw(st.integers(500, 5_000))
    else:
        kind = draw(st.one_of(covered, st.sampled_from(ActionKind)))
        claimed = draw(claimed_items(order)) if reason in ITEM_REASONS else ()
        amount = draw(st.integers(0, 30_000))
    return ProposedAction(kind=kind, reason=reason, amount=Money(amount), items=claimed)


@st.composite
def attempts(draw: st.DrawFn, orders: tuple[Order, ...]) -> Attempt:
    index = draw(st.integers(0, len(orders) - 1))
    proposal = draw(
        proposals(orders[index], plausible=draw(st.sampled_from([True, True, True, False])))
    )
    return Attempt(index, proposal, draw(st.booleans()), draw(st.integers(0, 60 * 24)))


@st.composite
def sequences(draw: st.DrawFn) -> tuple[tuple[Order, ...], tuple[Attempt, ...]]:
    orders = draw(customer_orders())
    steps = tuple(draw(st.lists(attempts(orders), min_size=5, max_size=15)))
    return orders, steps


FIRST_CLAIM = CLAIMED_AT
WINDOW = timedelta(days=30)
