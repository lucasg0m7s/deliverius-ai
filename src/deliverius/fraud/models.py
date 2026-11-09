"""What the fraud model reads about a claim and what it answers.

The facts are the ones the application also stores, so training and serving see the same thing.
Nothing here holds personal data: no name, address, document or message text.
"""

import math
from dataclasses import dataclass
from datetime import datetime

from deliverius.orders import PaymentMethod
from deliverius.policy import ActionKind, ClaimReason
from deliverius.shared.clock import require_aware
from deliverius.shared.errors import InvariantError
from deliverius.shared.ids import OrderId
from deliverius.shared.money import Money


@dataclass(frozen=True, slots=True)
class CustomerFacts:
    """Only the age of the account: who the customer is never becomes a feature."""

    created_at: datetime

    def __post_init__(self) -> None:
        require_aware(self.created_at, "created_at")


@dataclass(frozen=True, slots=True)
class PriorClaim:
    """A claim the policy did not deny: an approval request, or an action executed without one.

    `reviewed` says it went to a person; the reviewer's outcome is deliberately absent, because a
    rejection almost always means fraud and would leak the label.
    """

    at: datetime
    kind: ActionKind
    requested: Money
    reviewed: bool

    def __post_init__(self) -> None:
        require_aware(self.at, "at")


@dataclass(frozen=True, slots=True)
class CustomerClaimHistory:
    """The customer's facts around a claim. May hold later facts: the features ignore them."""

    customer: CustomerFacts
    order_placed_at: tuple[datetime, ...]
    claims: tuple[PriorClaim, ...]

    def __post_init__(self) -> None:
        for placed_at in self.order_placed_at:
            require_aware(placed_at, "order_placed_at")


@dataclass(frozen=True, slots=True)
class ClaimContext:
    """One claim to score: the order, the proposal, the instant and the customer's history.

    `delivered_at` is the instant of the order's first "delivered" event, if any, even a later one.
    """

    order_id: OrderId
    claimed_at: datetime
    kind: ActionKind
    reason: ClaimReason
    payment_method: PaymentMethod
    order_total: Money
    requested: Money
    delivered_at: datetime | None
    history: CustomerClaimHistory

    def __post_init__(self) -> None:
        require_aware(self.claimed_at, "claimed_at")
        if self.delivered_at is not None:
            require_aware(self.delivered_at, "delivered_at")


@dataclass(frozen=True, slots=True)
class FraudFactor:
    """A feature and its signed contribution to the raw score (log-odds); never a feature value."""

    feature: str
    contribution: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.contribution):
            raise InvariantError("fraud_factor.contribution_not_finite")


@dataclass(frozen=True, slots=True)
class FraudScore:
    """The calibrated probability, whether a person should review, and what weighed the most."""

    probability: float
    model_version: str
    needs_review: bool
    factors: tuple[FraudFactor, ...]

    def __post_init__(self) -> None:
        if not 0.0 <= self.probability <= 1.0:
            raise InvariantError("fraud_score.probability_out_of_range")
        if not self.model_version:
            raise InvariantError("fraud_score.model_version_empty")
