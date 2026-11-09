"""The feature schema is closed, versioned and free of personal or label fields."""

import pytest

from deliverius.fraud import features
from deliverius.fraud.features import (
    FEATURE_NAMES,
    FEATURE_SCHEMA,
    FEATURES_VERSION,
    Feature,
    schema_hash,
)

# Changing the schema changes this hash: bump `FEATURES_VERSION` and update both lines together.
GOLDEN_VERSION = "1.0.0"
GOLDEN_HASH = "2616a1e1f99103eef888ab3857584ad465f54863e4904435882812615c4709a2"

EXPECTED_NAMES = (
    "account_age_days",
    "orders_90d",
    "claims_30d",
    "claims_90d",
    "refund_requested_30d_cents",
    "claims_per_order_90d",
    "reviews_90d",
    "order_total_cents",
    "requested_cents",
    "requested_fraction",
    "minutes_since_delivery",
    "marked_delivered",
    "local_hour",
    "action_refund",
    "action_coupon",
    "action_cancellation",
    "reason_item_missing",
    "reason_wrong_item",
    "reason_quality_issue",
    "reason_late_delivery",
    "reason_not_delivered",
    "reason_duplicate_charge",
    "reason_customer_cancellation",
    "payment_pix",
    "payment_credit_card",
    "payment_debit_card",
    "payment_meal_voucher",
)
FORBIDDEN_FRAGMENTS = (
    "address",
    "zip",
    "cep",
    "district",
    "city",
    "name",
    "cpf",
    "phone",
    "email",
    "message",
    "profile",
    "label",
    "reviewer",
)
INDICATOR_PREFIXES = ("action_", "reason_", "payment_")


def test_the_schema_hash_is_the_golden_one_for_this_version() -> None:
    assert FEATURES_VERSION == GOLDEN_VERSION, "update GOLDEN_VERSION and GOLDEN_HASH together"
    assert schema_hash() == GOLDEN_HASH, "the schema changed: bump FEATURES_VERSION"


def test_the_schema_lists_exactly_the_versioned_features() -> None:
    assert FEATURE_NAMES == EXPECTED_NAMES
    assert len(set(FEATURE_NAMES)) == len(FEATURE_NAMES)


@pytest.mark.parametrize("name", EXPECTED_NAMES)
def test_no_feature_name_comes_from_a_forbidden_field(name: str) -> None:
    assert [fragment for fragment in FORBIDDEN_FRAGMENTS if fragment in name] == []


def test_indicators_are_marked_as_such() -> None:
    indicators = {feature.name for feature in FEATURE_SCHEMA if feature.kind == "indicator"}

    assert indicators == {name for name in EXPECTED_NAMES if name.startswith(INDICATOR_PREFIXES)}


@pytest.mark.parametrize(
    "schema",
    [
        FEATURE_SCHEMA[:-1],
        (*FEATURE_SCHEMA, Feature("extra", "numeric")),
        (FEATURE_SCHEMA[1], FEATURE_SCHEMA[0], *FEATURE_SCHEMA[2:]),
        (Feature(FEATURE_SCHEMA[0].name, "indicator"), *FEATURE_SCHEMA[1:]),
    ],
    ids=["removed", "added", "reordered", "kind_changed"],
)
def test_any_change_to_the_schema_changes_the_hash(
    monkeypatch: pytest.MonkeyPatch, schema: tuple[Feature, ...]
) -> None:
    monkeypatch.setattr(features, "FEATURE_SCHEMA", schema)

    assert schema_hash() != GOLDEN_HASH


def test_a_new_version_changes_the_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(features, "FEATURES_VERSION", "1.0.1")

    assert schema_hash() != GOLDEN_HASH
