"""The features of a claim, computed point in time: the one code shared by training and serving.

Every fact counts only if its instant is strictly before the claim, and this module applies that
filter itself instead of trusting the caller: a history that also holds later facts gives the same
vector. Every value is a float; an absent one is NaN. Categories become 0/1 indicators of closed
enums, so the logistic regression and LightGBM read the same vector and no category map can drift.
"""

import hashlib
import json
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Final, Literal

from deliverius.fraud.models import ClaimContext, PriorClaim
from deliverius.orders import PaymentMethod
from deliverius.policy import ActionKind, ClaimReason

# Bump on any change to a name, the order, a kind or a computation: the model's manifest carries
# the hash, and a model trained on another schema is refused.
FEATURES_VERSION: Final = "1.0.0"

SHORT_WINDOW: Final = timedelta(days=30)
LONG_WINDOW: Final = timedelta(days=90)
# The reader fetches at most this many rows per query; the training side saturates the same way.
MAX_ROWS_PER_WINDOW: Final = 1_000
# Brazil has had no daylight saving time since 2019: a fixed offset avoids depending on tzdata.
LOCAL_TIME: Final = timezone(timedelta(hours=-3))

type FeatureKind = Literal["numeric", "indicator"]
type FeatureVector = tuple[float, ...]


@dataclass(frozen=True, slots=True)
class Feature:
    name: str
    kind: FeatureKind


_NUMERIC_NAMES: Final = (
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
)

FEATURE_SCHEMA: Final = (
    *(Feature(name, "numeric") for name in _NUMERIC_NAMES),
    *(Feature(f"action_{kind.value}", "indicator") for kind in ActionKind),
    *(Feature(f"reason_{reason.value}", "indicator") for reason in ClaimReason),
    *(Feature(f"payment_{method.value}", "indicator") for method in PaymentMethod),
)
FEATURE_NAMES: Final = tuple(feature.name for feature in FEATURE_SCHEMA)


def schema_hash() -> str:
    """SHA-256 of the version, names, order and kinds, as canonical JSON."""
    canonical = json.dumps(
        {
            "version": FEATURES_VERSION,
            "features": [[feature.name, feature.kind] for feature in FEATURE_SCHEMA],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_features(context: ClaimContext) -> FeatureVector:
    """The vector of `context`, in the order of `FEATURE_SCHEMA`."""
    claimed_at = context.claimed_at
    history = context.history
    orders_90d = len(_recent(history.order_placed_at, claimed_at))
    claims_90d = _recent_claims(history.claims, claimed_at)
    claims_30d = [claim for claim in claims_90d if claim.at >= claimed_at - SHORT_WINDOW]
    delivered_at = _strictly_before(context.delivered_at, claimed_at)
    numeric = (
        max(0.0, _days(claimed_at - history.customer.created_at)),
        float(orders_90d),
        float(len(claims_30d)),
        float(len(claims_90d)),
        float(sum(c.requested.cents for c in claims_30d if c.kind is ActionKind.REFUND)),
        _ratio(len(claims_90d), orders_90d, when_zero=0.0),
        float(sum(1 for claim in claims_90d if claim.reviewed)),
        float(context.order_total.cents),
        float(context.requested.cents),
        _ratio(context.requested.cents, context.order_total.cents, when_zero=math.nan),
        math.nan if delivered_at is None else _minutes(claimed_at - delivered_at),
        0.0 if delivered_at is None else 1.0,
        float(claimed_at.astimezone(LOCAL_TIME).hour),
    )
    return (
        *numeric,
        *_indicators(ActionKind, context.kind),
        *_indicators(ClaimReason, context.reason),
        *_indicators(PaymentMethod, context.payment_method),
    )


def _recent(instants: Iterable[datetime], claimed_at: datetime) -> list[datetime]:
    """Instants in `[claimed_at - 90 d, claimed_at)`, the most recent first, at most the ceiling."""
    start = claimed_at - LONG_WINDOW
    inside = sorted((at for at in instants if start <= at < claimed_at), reverse=True)
    return inside[:MAX_ROWS_PER_WINDOW]


def _recent_claims(claims: Sequence[PriorClaim], claimed_at: datetime) -> list[PriorClaim]:
    start = claimed_at - LONG_WINDOW
    inside = sorted(
        (claim for claim in claims if start <= claim.at < claimed_at),
        key=lambda claim: claim.at,
        reverse=True,
    )
    return inside[:MAX_ROWS_PER_WINDOW]


def _strictly_before(moment: datetime | None, claimed_at: datetime) -> datetime | None:
    if moment is None or moment >= claimed_at:
        return None
    return moment


def _ratio(numerator: int, denominator: int, *, when_zero: float) -> float:
    return when_zero if denominator == 0 else numerator / denominator


def _days(delta: timedelta) -> float:
    return delta.total_seconds() / 86_400


def _minutes(delta: timedelta) -> float:
    return delta.total_seconds() / 60


def _indicators(members: type[StrEnum], value: StrEnum) -> tuple[float, ...]:
    return tuple(1.0 if member is value else 0.0 for member in members)
