"""The review rule (`p x amount >= review cost`) and the factors that weighed the most."""

import pytest

from deliverius.fraud.decision import needs_review, top_factors
from deliverius.fraud.models import FraudFactor
from deliverius.shared.money import Money

REVIEW_COST = Money(400)
NAMES = ("a", "b", "c", "d", "e")


def test_a_medium_score_on_a_small_amount_is_not_reviewed() -> None:
    # 0.30 x 8.00 = 2.40, below the 4.00 of a review.
    assert needs_review(0.30, Money(800), REVIEW_COST) is False


def test_the_same_score_on_a_large_amount_is_reviewed() -> None:
    # 0.30 x 180.00 = 54.00, above the 4.00 of a review.
    assert needs_review(0.30, Money(18_000), REVIEW_COST) is True


@pytest.mark.parametrize(("probability", "cents"), [(0.5, 800), (0.25, 1_600), (1.0, 400)])
def test_an_expected_loss_equal_to_the_cost_is_reviewed(probability: float, cents: int) -> None:
    assert needs_review(probability, Money(cents), REVIEW_COST) is True


def test_just_below_the_cost_is_not_reviewed() -> None:
    assert needs_review(0.4999, Money(800), REVIEW_COST) is False


def test_a_zero_probability_is_never_reviewed() -> None:
    assert needs_review(0.0, Money(1_000_000), REVIEW_COST) is False


def test_the_three_largest_by_absolute_value_with_their_sign() -> None:
    factors = top_factors(NAMES, (0.1, -2.0, 0.5, 1.5, -0.2))

    assert factors == (
        FraudFactor("b", -2.0),
        FraudFactor("d", 1.5),
        FraudFactor("c", 0.5),
    )


def test_a_tie_keeps_the_schema_order() -> None:
    factors = top_factors(NAMES, (0.3, -0.3, 0.3, 0.0, 0.3))

    assert [factor.feature for factor in factors] == ["a", "b", "c"]


def test_fewer_features_than_requested_returns_them_all() -> None:
    assert top_factors(("a", "b"), (1.0, 2.0)) == (FraudFactor("b", 2.0), FraudFactor("a", 1.0))


def test_names_and_contributions_must_have_the_same_length() -> None:
    with pytest.raises(ValueError, match="zip"):
        top_factors(("a", "b"), (1.0,))
