from datetime import datetime

import pytest
from support.fraud_builders import CLAIMED_AT, claim_ctx

from deliverius.fraud.models import (
    CustomerClaimHistory,
    CustomerFacts,
    FraudFactor,
    FraudScore,
    PriorClaim,
)
from deliverius.policy import ActionKind
from deliverius.shared.errors import InvariantError
from deliverius.shared.money import Money

NAIVE = datetime(2026, 10, 1, 12, 0)  # noqa: DTZ001 - the invalid input under test


def test_the_account_creation_needs_a_time_zone() -> None:
    with pytest.raises(InvariantError, match=r"naive_datetime\.created_at"):
        CustomerFacts(NAIVE)


def test_a_prior_claim_needs_a_time_zone() -> None:
    with pytest.raises(InvariantError, match=r"naive_datetime\.at"):
        PriorClaim(NAIVE, ActionKind.REFUND, Money(100), reviewed=False)


def test_every_order_instant_needs_a_time_zone() -> None:
    with pytest.raises(InvariantError, match=r"naive_datetime\.order_placed_at"):
        CustomerClaimHistory(CustomerFacts(CLAIMED_AT), (CLAIMED_AT, NAIVE), ())


def test_the_claim_instant_needs_a_time_zone() -> None:
    with pytest.raises(InvariantError, match=r"naive_datetime\.claimed_at"):
        claim_ctx(claimed_at=NAIVE)


def test_the_delivery_instant_needs_a_time_zone_when_present() -> None:
    with pytest.raises(InvariantError, match=r"naive_datetime\.delivered_at"):
        claim_ctx(delivered_at=NAIVE)

    assert claim_ctx(delivered_at=None).delivered_at is None


@pytest.mark.parametrize("probability", [0.0, 0.5, 1.0])
def test_a_probability_between_zero_and_one_is_accepted(probability: float) -> None:
    score = FraudScore(probability, "v1.0.0", needs_review=False, factors=())

    assert score.probability == probability


@pytest.mark.parametrize("probability", [-0.01, 1.01, float("nan")])
def test_a_probability_outside_zero_and_one_is_refused(probability: float) -> None:
    with pytest.raises(InvariantError, match=r"fraud_score\.probability_out_of_range"):
        FraudScore(probability, "v1.0.0", needs_review=False, factors=())


def test_a_score_names_its_model_version() -> None:
    with pytest.raises(InvariantError, match=r"fraud_score\.model_version_empty"):
        FraudScore(0.2, "", needs_review=False, factors=())


@pytest.mark.parametrize("contribution", [float("nan"), float("inf")])
def test_a_factor_contribution_is_a_finite_number(contribution: float) -> None:
    with pytest.raises(InvariantError, match=r"fraud_factor\.contribution_not_finite"):
        FraudFactor("claims_30d", contribution)


def test_a_score_keeps_its_factors_in_order() -> None:
    factors = (FraudFactor("claims_30d", 1.2), FraudFactor("requested_fraction", -0.4))

    score = FraudScore(0.7, "v1.0.0", needs_review=True, factors=factors)

    assert score.factors == factors
