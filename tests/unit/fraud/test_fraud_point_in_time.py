"""No feature uses a fact from the claim instant or later: the "Pronto quando" proof of the change.

Hypothesis builds a history, then adds facts at the claim instant and after it (orders, claims and
a late delivery event). The two vectors must be identical, value for value.
"""

import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from random import Random

from hypothesis import given, settings
from hypothesis import strategies as st

from deliverius.fraud.features import FeatureVector, compute_features
from deliverius.fraud.models import ClaimContext, CustomerClaimHistory, CustomerFacts, PriorClaim
from deliverius.orders import PaymentMethod
from deliverius.policy import ActionKind, ClaimReason
from deliverius.shared.ids import OrderId, new_id
from deliverius.shared.money import Money

CLAIMED_AT = datetime(2026, 10, 15, 21, 30, tzinfo=UTC)
# Seconds before (negative) or after the claim; zero is the claim instant itself.
_BEFORE = st.integers(min_value=-200 * 86_400, max_value=-1)
_AT_OR_AFTER = st.integers(min_value=0, max_value=200 * 86_400)


def _at(seconds: int) -> datetime:
    return CLAIMED_AT + timedelta(seconds=seconds)


def _claims(offsets: st.SearchStrategy[int]) -> st.SearchStrategy[list[PriorClaim]]:
    one = st.builds(
        PriorClaim,
        offsets.map(_at),
        st.sampled_from(ActionKind),
        st.integers(1, 50_000).map(Money),
        st.booleans(),
    )
    return st.lists(one, max_size=25)


def _same(left: FeatureVector, right: FeatureVector) -> bool:
    return len(left) == len(right) and all(
        a == b or (math.isnan(a) and math.isnan(b)) for a, b in zip(left, right, strict=True)
    )


@st.composite
def contexts(draw: st.DrawFn) -> ClaimContext:
    history = CustomerClaimHistory(
        customer=CustomerFacts(_at(draw(st.integers(-800 * 86_400, -1)))),
        order_placed_at=tuple(draw(st.lists(_BEFORE.map(_at), max_size=25))),
        claims=tuple(draw(_claims(_BEFORE))),
    )
    delivered = draw(st.none() | _BEFORE.map(_at))
    return ClaimContext(
        order_id=OrderId(new_id()),
        claimed_at=CLAIMED_AT,
        kind=draw(st.sampled_from(ActionKind)),
        reason=draw(st.sampled_from(ClaimReason)),
        payment_method=draw(st.sampled_from(PaymentMethod)),
        order_total=Money(draw(st.integers(0, 50_000))),
        requested=Money(draw(st.integers(1, 50_000))),
        delivered_at=delivered,
        history=history,
    )


@settings(max_examples=300)
@given(
    context=contexts(),
    later_orders=st.lists(_AT_OR_AFTER.map(_at), max_size=25),
    later_claims=_claims(_AT_OR_AFTER),
    late_delivery=_AT_OR_AFTER.map(_at),
    shuffle=st.randoms(use_true_random=False),
)
def test_facts_at_or_after_the_claim_never_change_the_vector(
    context: ClaimContext,
    later_orders: list[datetime],
    later_claims: list[PriorClaim],
    late_delivery: datetime,
    shuffle: Random,
) -> None:
    history = context.history
    orders = [*history.order_placed_at, *later_orders]
    claims = [*history.claims, *later_claims]
    shuffle.shuffle(orders)
    shuffle.shuffle(claims)
    widened = replace(
        context,
        history=replace(history, order_placed_at=tuple(orders), claims=tuple(claims)),
        # A delivery the order only got later must look like no delivery at all.
        delivered_at=context.delivered_at or late_delivery,
    )

    assert _same(compute_features(context), compute_features(widened))
