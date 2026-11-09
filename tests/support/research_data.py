"""Small datasets and tables for the research tests: the real dataset is not in the repository."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
from support.order_builders import build_order

from deliverius.datagen.output import write_dataset
from deliverius.datagen.records import (
    ClaimRecord,
    CustomerRecord,
    DecisionKind,
    GroundTruthRecord,
    Manifest,
    OrderRecord,
    Profile,
    ResolutionRecord,
)
from deliverius.fraud.features import FEATURE_NAMES
from deliverius.orders import Customer, Order
from deliverius.policy import (
    ActionKind,
    ApprovalReason,
    ClaimReason,
    DenialReason,
    PolicyDocumentId,
)
from deliverius.shared.ids import CustomerId, SandboxId, new_id
from research.fraud.dataset import ClaimTable

SANDBOX = SandboxId(new_id())
CREATED_AT = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)


def customer(customer_id: CustomerId) -> CustomerRecord:
    return CustomerRecord.from_domain(
        Customer(
            id=customer_id,
            sandbox_id=SANDBOX,
            created_at=CREATED_AT,
            name="Cliente Teste",
            cpf="00000000000",
            phone="+55 11 90000-0000",
            email="cliente@example.com",
            address="Rua Exemplo, 1",
        )
    )


def order_of(customer_id: CustomerId, placed_at: datetime) -> Order:
    return build_order(sandbox_id=SANDBOX, customer_id=customer_id, placed_at=placed_at)


def resolution(decision: DecisionKind) -> ResolutionRecord:
    match decision:
        case DecisionKind.ALLOWED:
            return ResolutionRecord(decision=decision, document=PolicyDocumentId.POL_03)
        case DecisionKind.NEEDS_APPROVAL:
            return ResolutionRecord(
                decision=decision, approval_reasons=(ApprovalReason.EXCEEDS_ACTION_LIMIT,)
            )
        case DecisionKind.DENIED:
            return ResolutionRecord(decision=decision, denial_reason=DenialReason.NOT_COVERED)


def claim(
    order: Order,
    at: datetime,
    decision: DecisionKind = DecisionKind.ALLOWED,
    *,
    cents: int = 2_000,
    fraud: bool = False,
) -> ClaimRecord:
    return ClaimRecord(
        id=new_id(),
        order_id=order.id,
        customer_id=order.customer_id,
        claimed_at=at,
        reason=ClaimReason.ITEM_MISSING,
        action=ActionKind.REFUND,
        claimed_items=(),
        requested_amount_cents=cents,
        resolution=resolution(decision),
        is_fraud=fraud,
    )


def write_small_dataset(directory: Path, claims: list[ClaimRecord], orders: list[Order]) -> Path:
    """A dataset folder with the files the research reads, and a ground truth it must not parse."""
    customers = {order.customer_id for order in orders}
    cutoff = datetime(2026, 10, 1, tzinfo=UTC)
    write_dataset(
        directory,
        {
            "fraud_history/train.jsonl": [c for c in claims if c.claimed_at < cutoff],
            "fraud_history/test.jsonl": [c for c in claims if c.claimed_at >= cutoff],
            "fraud_ground_truth/all.jsonl": [
                GroundTruthRecord(
                    claim_id=c.id,
                    customer_id=c.customer_id,
                    profile=Profile.HONEST,
                    label_flipped=False,
                    incident=None,
                    exaggerated=False,
                )
                for c in claims
            ],
            "world/customers.jsonl": [customer(c) for c in sorted(customers)],
            "world/orders.jsonl": [OrderRecord.from_domain(order) for order in orders],
        },
        Manifest(datagen_version="test", seed=1, commit="test", reference_instant=cutoff),
    )
    return directory


def synthetic_table(rows_per_month: int = 120, seed: int = 7) -> ClaimTable:
    """A table from May to October with a learnable signal in `claims_30d`; no contexts."""
    rng = np.random.default_rng(seed)
    claimed_at: list[datetime] = []
    for month in range(5, 11):
        start = datetime(2026, month, 1, tzinfo=UTC)
        claimed_at += [
            start + timedelta(hours=int(h)) for h in rng.integers(0, 24 * 28, rows_per_month)
        ]
    claimed_at.sort()
    rows = len(claimed_at)
    features = rng.normal(size=(rows, len(FEATURE_NAMES)))
    claims_30d = rng.poisson(1.0, rows).astype(np.float64)
    features[:, FEATURE_NAMES.index("claims_30d")] = claims_30d
    features[rng.random(rows) < 0.1, FEATURE_NAMES.index("minutes_since_delivery")] = np.nan
    logit = -2.5 + 0.9 * claims_30d + 0.5 * features[:, 0]
    is_fraud = (rng.random(rows) < 1 / (1 + np.exp(-logit))).astype(np.int64)
    return ClaimTable(
        claim_ids=tuple(new_id() for _ in range(rows)),
        claimed_at=tuple(claimed_at),
        contexts=(),
        features=features,
        is_fraud=is_fraud,
        requested_cents=rng.integers(300, 20_000, rows).astype(np.int64),
    )
