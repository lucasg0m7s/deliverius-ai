"""The two decisions taken after the model: whether a person reviews, and what weighed the most."""

from collections.abc import Sequence
from fractions import Fraction
from typing import Final

from deliverius.fraud.models import FraudFactor
from deliverius.shared.money import Money

TOP_FACTORS: Final = 3


def needs_review(probability: float, requested: Money, review_cost: Money) -> bool:
    """Review when the expected loss of paying (`p x amount`) reaches the cost of a review.

    Exact arithmetic: a float product would put the "equal to the cost" case on either side.
    """
    expected_loss = Fraction(probability) * requested.cents
    return expected_loss >= review_cost.cents


def top_factors(
    names: Sequence[str], contributions: Sequence[float], count: int = TOP_FACTORS
) -> tuple[FraudFactor, ...]:
    """The `count` largest contributions by absolute value, with their sign.

    `contributions` holds one value per feature, in the order of `names`, without the bias term.
    A tie keeps the schema order (a stable sort).
    """
    ranked = sorted(zip(names, contributions, strict=True), key=lambda pair: -abs(pair[1]))
    return tuple(FraudFactor(name, value) for name, value in ranked[:count])
