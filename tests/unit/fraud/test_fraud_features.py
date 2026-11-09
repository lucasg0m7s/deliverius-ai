"""The features of a claim, scenario by scenario (spec "Features no ponto do tempo")."""

import math
from datetime import UTC, datetime, timedelta

import pytest
from support.fraud_builders import ACCOUNT_CREATED_AT, CLAIMED_AT, claim_ctx, claim_history
from support.fraud_builders import prior_claim as claim

from deliverius.fraud.features import (
    FEATURE_NAMES,
    MAX_ROWS_PER_WINDOW,
    FeatureVector,
    compute_features,
)
from deliverius.orders import PaymentMethod
from deliverius.policy import ActionKind, ClaimReason


def named(vector: FeatureVector) -> dict[str, float]:
    return dict(zip(FEATURE_NAMES, vector, strict=True))


def test_the_vector_has_one_value_per_feature_of_the_schema() -> None:
    assert len(compute_features(claim_ctx())) == len(FEATURE_NAMES)


def test_a_customer_without_history_has_zero_counts_and_no_division_by_zero() -> None:
    features = named(compute_features(claim_ctx()))

    for name in (
        "orders_90d",
        "claims_30d",
        "claims_90d",
        "refund_requested_30d_cents",
        "claims_per_order_90d",
        "reviews_90d",
    ):
        assert features[name] == 0.0, name


def test_counts_use_the_30_and_90_day_windows() -> None:
    history = claim_history(
        order_days_before=(1, 20, 60, 89.9, 91),
        claims=(
            claim(2, cents=1_000),
            claim(29, cents=500, kind=ActionKind.COUPON),
            claim(31, cents=700, reviewed=True),
            claim(89, reviewed=True),
            claim(120, reviewed=True),
        ),
    )

    features = named(compute_features(claim_ctx(history=history)))

    assert features["orders_90d"] == 4.0
    assert features["claims_30d"] == 2.0
    assert features["claims_90d"] == 4.0
    assert features["refund_requested_30d_cents"] == 1_000.0
    assert features["claims_per_order_90d"] == 1.0
    assert features["reviews_90d"] == 2.0


def test_the_window_start_is_inside_and_the_claim_instant_is_outside() -> None:
    history = claim_history(
        order_days_before=(90, 0),
        claims=(claim(30), claim(90), claim(0)),
    )

    features = named(compute_features(claim_ctx(history=history)))

    assert features["orders_90d"] == 1.0
    assert features["claims_30d"] == 1.0
    assert features["claims_90d"] == 2.0


def test_another_claim_at_the_same_instant_is_not_a_prior_claim() -> None:
    history = claim_history(claims=(claim(0), claim(0, reviewed=True)))

    features = named(compute_features(claim_ctx(history=history)))

    assert features["claims_30d"] == 0.0
    assert features["claims_90d"] == 0.0
    assert features["reviews_90d"] == 0.0


def test_a_delivery_marked_after_the_claim_is_absent() -> None:
    context = claim_ctx(delivered_at=CLAIMED_AT + timedelta(minutes=5))

    features = named(compute_features(context))

    assert features["marked_delivered"] == 0.0
    assert math.isnan(features["minutes_since_delivery"])


def test_a_delivery_marked_at_the_claim_instant_is_absent() -> None:
    features = named(compute_features(claim_ctx(delivered_at=CLAIMED_AT)))

    assert features["marked_delivered"] == 0.0
    assert math.isnan(features["minutes_since_delivery"])


def test_a_delivery_before_the_claim_gives_the_minutes_since() -> None:
    features = named(compute_features(claim_ctx(delivered_at=CLAIMED_AT - timedelta(hours=2))))

    assert features["marked_delivered"] == 1.0
    assert features["minutes_since_delivery"] == 120.0


def test_an_order_never_delivered_has_no_delivery_features() -> None:
    features = named(compute_features(claim_ctx(delivered_at=None)))

    assert features["marked_delivered"] == 0.0
    assert math.isnan(features["minutes_since_delivery"])


def test_the_order_values_and_the_requested_fraction() -> None:
    features = named(compute_features(claim_ctx(total_cents=8_000, requested_cents=6_000)))

    assert features["order_total_cents"] == 8_000.0
    assert features["requested_cents"] == 6_000.0
    assert features["requested_fraction"] == 0.75


def test_a_free_order_has_no_requested_fraction() -> None:
    features = named(compute_features(claim_ctx(total_cents=0, requested_cents=100)))

    assert math.isnan(features["requested_fraction"])


def test_the_account_age_is_in_days() -> None:
    history = claim_history(created_at=CLAIMED_AT - timedelta(days=10, hours=12))

    features = named(compute_features(claim_ctx(history=history)))

    assert features["account_age_days"] == 10.5


def test_an_account_created_after_the_claim_has_age_zero() -> None:
    history = claim_history(created_at=CLAIMED_AT + timedelta(days=1))

    assert named(compute_features(claim_ctx(history=history)))["account_age_days"] == 0.0


@pytest.mark.parametrize(("utc_hour", "local_hour"), [(21, 18.0), (2, 23.0), (3, 0.0), (15, 12.0)])
def test_the_hour_is_local_time_utc_minus_3(utc_hour: int, local_hour: float) -> None:
    claimed_at = datetime(2026, 10, 15, utc_hour, 59, tzinfo=UTC)
    history = claim_history(created_at=ACCOUNT_CREATED_AT, claimed_at=claimed_at)

    context = claim_ctx(claimed_at=claimed_at, history=history, delivered_at=None)

    assert named(compute_features(context))["local_hour"] == local_hour


def test_the_categories_become_one_hot_indicators() -> None:
    context = claim_ctx(
        kind=ActionKind.COUPON,
        reason=ClaimReason.LATE_DELIVERY,
        payment_method=PaymentMethod.MEAL_VOUCHER,
    )

    features = named(compute_features(context))

    hot = {
        name
        for name, value in features.items()
        if name.startswith(("action_", "reason_", "payment_")) and value == 1.0
    }
    assert hot == {"action_coupon", "reason_late_delivery", "payment_meal_voucher"}
    for prefix in ("action_", "reason_", "payment_"):
        group = [value for name, value in features.items() if name.startswith(prefix)]
        assert sorted(set(group)) == [0.0, 1.0]


def test_the_counts_saturate_at_the_row_ceiling() -> None:
    too_many = MAX_ROWS_PER_WINDOW + 50
    history = claim_history(
        order_days_before=tuple(1 + day / too_many for day in range(too_many)),
        claims=tuple(claim(1 + day / too_many, reviewed=True) for day in range(too_many)),
    )

    features = named(compute_features(claim_ctx(history=history)))

    assert features["orders_90d"] == MAX_ROWS_PER_WINDOW
    assert features["claims_90d"] == MAX_ROWS_PER_WINDOW
    assert features["claims_30d"] == MAX_ROWS_PER_WINDOW
    assert features["reviews_90d"] == MAX_ROWS_PER_WINDOW


def test_the_ceiling_keeps_the_most_recent_claims() -> None:
    recent = tuple(claim(1 + day / 2_000) for day in range(MAX_ROWS_PER_WINDOW))
    old_reviewed = tuple(claim(80, reviewed=True) for _ in range(10))
    history = claim_history(claims=old_reviewed + recent)

    features = named(compute_features(claim_ctx(history=history)))

    assert features["claims_90d"] == MAX_ROWS_PER_WINDOW
    assert features["reviews_90d"] == 0.0
