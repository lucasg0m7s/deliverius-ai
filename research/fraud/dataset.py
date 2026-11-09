"""The training table: one row per claim the policy did not deny, with the application's features.

The dataset is checked against its manifest before anything else. Each claim gets the history the
application would read from Postgres (account, orders, earlier claims that were not denied) and
goes through the same `claim_context` and `compute_features` as the scorer. The ground-truth file
(profile, incident, flipped label) is never parsed.
"""

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final
from uuid import UUID

import numpy as np
from numpy.typing import NDArray

from deliverius.datagen.output import read_records, verify_dataset
from deliverius.datagen.records import (
    ClaimRecord,
    CustomerRecord,
    DecisionKind,
    OrderRecord,
)
from deliverius.fraud.context import claim_context
from deliverius.fraud.features import compute_features
from deliverius.fraud.models import ClaimContext, CustomerClaimHistory, CustomerFacts, PriorClaim
from deliverius.policy import ProposedAction
from deliverius.shared.money import Money

CLAIM_FILES: Final = ("fraud_history/train.jsonl", "fraud_history/test.jsonl")
CUSTOMERS_FILE: Final = "world/customers.jsonl"
ORDERS_FILE: Final = "world/orders.jsonl"


@dataclass(frozen=True)
class ClaimTable:
    """Rows in claim order (instant, then id); `features` follows `FEATURE_SCHEMA`."""

    claim_ids: tuple[UUID, ...]
    claimed_at: tuple[datetime, ...]
    contexts: tuple[ClaimContext, ...]
    features: NDArray[np.float64]
    is_fraud: NDArray[np.int64]
    requested_cents: NDArray[np.int64]

    def __len__(self) -> int:
        return len(self.claim_ids)

    def subset(self, mask: NDArray[np.bool_]) -> "ClaimTable":
        rows = np.flatnonzero(mask)
        return ClaimTable(
            claim_ids=tuple(self.claim_ids[row] for row in rows),
            claimed_at=tuple(self.claimed_at[row] for row in rows),
            contexts=tuple(self.contexts[row] for row in rows),
            features=self.features[rows],
            is_fraud=self.is_fraud[rows],
            requested_cents=self.requested_cents[rows],
        )


def load_claim_table(directory: Path) -> ClaimTable:
    """Verify the whole dataset, then build the table. A tampered file stops before any feature."""
    verify_dataset(directory)
    claims = [claim for name in CLAIM_FILES for claim in read_records(directory, name, ClaimRecord)]
    customers = read_records(directory, CUSTOMERS_FILE, CustomerRecord)
    orders = read_records(directory, ORDERS_FILE, OrderRecord)
    return build_claim_table(claims, customers, orders)


def build_claim_table(
    claims: Iterable[ClaimRecord],
    customers: Iterable[CustomerRecord],
    orders: Iterable[OrderRecord],
) -> ClaimTable:
    scored = sorted(
        (claim for claim in claims if claim.resolution.decision is not DecisionKind.DENIED),
        key=lambda claim: (claim.claimed_at, claim.id),
    )
    created_at = {customer.id: customer.created_at for customer in customers}
    orders_by_id = {order.id: order for order in orders}
    placed_at: defaultdict[UUID, list[datetime]] = defaultdict(list)
    for order in orders_by_id.values():
        placed_at[order.customer_id].append(order.placed_at)
    prior: defaultdict[UUID, list[PriorClaim]] = defaultdict(list)
    for claim in scored:
        prior[claim.customer_id].append(as_prior_claim(claim))

    contexts = tuple(
        claim_context(
            orders_by_id[claim.order_id].to_domain(),
            ProposedAction(claim.action, claim.reason, Money(claim.requested_amount_cents)),
            claim.claimed_at,
            # The whole history of the customer: `compute_features` keeps only what came before.
            CustomerClaimHistory(
                CustomerFacts(created_at[claim.customer_id]),
                tuple(placed_at[claim.customer_id]),
                tuple(prior[claim.customer_id]),
            ),
        )
        for claim in scored
    )
    return ClaimTable(
        claim_ids=tuple(claim.id for claim in scored),
        claimed_at=tuple(claim.claimed_at for claim in scored),
        contexts=contexts,
        features=feature_matrix(contexts),
        is_fraud=np.array([int(claim.is_fraud) for claim in scored], dtype=np.int64),
        requested_cents=np.array(
            [claim.requested_amount_cents for claim in scored], dtype=np.int64
        ),
    )


def as_prior_claim(claim: ClaimRecord) -> PriorClaim:
    """A claim as the application stores it: the policy decision says whether it went to review."""
    return PriorClaim(
        at=claim.claimed_at,
        kind=claim.action,
        requested=Money(claim.requested_amount_cents),
        reviewed=claim.resolution.decision is DecisionKind.NEEDS_APPROVAL,
    )


def feature_matrix(contexts: Sequence[ClaimContext]) -> NDArray[np.float64]:
    return np.array([compute_features(context) for context in contexts], dtype=np.float64)
