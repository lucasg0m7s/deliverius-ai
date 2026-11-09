"""Builders of fraud contexts for tests: a claim with a history around it."""

from datetime import UTC, datetime, timedelta

from deliverius.fraud.features import FEATURE_NAMES
from deliverius.fraud.models import ClaimContext, CustomerClaimHistory, CustomerFacts, PriorClaim
from deliverius.fraud.scorer import FraudModel, LogisticModel, SigmoidCalibrator
from deliverius.orders import PaymentMethod
from deliverius.policy import ActionKind, ClaimReason
from deliverius.shared.ids import OrderId, new_id
from deliverius.shared.money import Money

CLAIMED_AT = datetime(2026, 10, 15, 21, 30, tzinfo=UTC)
ACCOUNT_CREATED_AT = CLAIMED_AT - timedelta(days=400)


def prior_claim(
    days_before: float,
    *,
    kind: ActionKind = ActionKind.REFUND,
    cents: int = 2_000,
    reviewed: bool = False,
    claimed_at: datetime = CLAIMED_AT,
) -> PriorClaim:
    return PriorClaim(claimed_at - timedelta(days=days_before), kind, Money(cents), reviewed)


def claim_history(
    *,
    order_days_before: tuple[float, ...] = (),
    claims: tuple[PriorClaim, ...] = (),
    created_at: datetime = ACCOUNT_CREATED_AT,
    claimed_at: datetime = CLAIMED_AT,
) -> CustomerClaimHistory:
    return CustomerClaimHistory(
        customer=CustomerFacts(created_at),
        order_placed_at=tuple(claimed_at - timedelta(days=days) for days in order_days_before),
        claims=claims,
    )


def claim_ctx(
    *,
    history: CustomerClaimHistory | None = None,
    claimed_at: datetime = CLAIMED_AT,
    kind: ActionKind = ActionKind.REFUND,
    reason: ClaimReason = ClaimReason.ITEM_MISSING,
    payment_method: PaymentMethod = PaymentMethod.PIX,
    total_cents: int = 8_000,
    requested_cents: int = 2_000,
    delivered_at: datetime | None = CLAIMED_AT - timedelta(minutes=45),
) -> ClaimContext:
    return ClaimContext(
        order_id=OrderId(new_id()),
        claimed_at=claimed_at,
        kind=kind,
        reason=reason,
        payment_method=payment_method,
        order_total=Money(total_cents),
        requested=Money(requested_cents),
        delivered_at=delivered_at,
        history=history if history is not None else claim_history(claimed_at=claimed_at),
    )


def logistic_model(
    *,
    coefficients: tuple[float, ...] | None = None,
    intercept: float = -2.0,
) -> LogisticModel:
    """A hand-made model over the real schema: claims and the requested fraction raise the score."""
    size = len(FEATURE_NAMES)
    weights = dict.fromkeys(FEATURE_NAMES, 0.0) | {
        "claims_30d": 0.9,
        "requested_fraction": 0.6,
        "account_age_days": -0.001,
        "minutes_since_delivery": 0.1,
    }
    return LogisticModel(
        medians=(1.0,) * size,
        means=(0.5,) * size,
        scales=(2.0,) * size,
        coefficients=coefficients or tuple(weights[name] for name in FEATURE_NAMES),
        intercept=intercept,
    )


def fraud_model(
    logistic: LogisticModel | None = None,
    *,
    a: float = -1.0,
    b: float = 0.0,
    review_cost_cents: int = 400,
    version: str = "1.0.0",
) -> FraudModel:
    return FraudModel(
        version=version,
        logistic=logistic or logistic_model(),
        calibrator=SigmoidCalibrator(a, b),
        review_cost=Money(review_cost_cents),
    )
