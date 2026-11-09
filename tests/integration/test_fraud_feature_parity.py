"""Training and serving see the same claim: the dataset path and the Postgres path give one vector.

The same customer history is written twice: as dataset records, read by `research.fraud.dataset`,
and as the rows the application stores (an approval request for a claim that went to review, an
automatic action for an allowed one, nothing for a denied one), read by the Postgres reader.
"""

import math
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert, update
from sqlalchemy.ext.asyncio import AsyncSession
from support.action_rows import action_values, approval_values, decision_values
from support.order_builders import build_order
from support.order_rows import insert_orders

from deliverius.datagen.records import (
    ClaimRecord,
    CustomerRecord,
    DecisionKind,
    OrderRecord,
    ResolutionRecord,
)
from deliverius.db.tables import ActionRow, ApprovalRequestRow, ConversationRow, CustomerRow
from deliverius.fraud.context import claim_context
from deliverius.fraud.features import compute_features
from deliverius.fraud.repository import PostgresFraudContextReader
from deliverius.orders import Customer, Order
from deliverius.policy import (
    ActionKind,
    ApprovalReason,
    ClaimReason,
    DenialReason,
    PolicyDocumentId,
    ProposedAction,
)
from deliverius.shared.access import CustomerScope
from deliverius.shared.ids import CustomerId, SandboxId, new_id
from deliverius.shared.money import Money
from research.fraud.dataset import build_claim_table

pytestmark = pytest.mark.integration

CLAIMED_AT = datetime(2026, 10, 20, 23, 10, tzinfo=UTC)
CREATED_AT = datetime(2026, 2, 14, 8, 0, tzinfo=UTC)
# (days before the claim, policy decision, action kind, requested cents)
PRIOR_CLAIMS = (
    (2.0, DecisionKind.ALLOWED, ActionKind.REFUND, 1_200),
    (6.5, DecisionKind.NEEDS_APPROVAL, ActionKind.REFUND, 9_800),
    (12.0, DecisionKind.DENIED, ActionKind.REFUND, 5_000),
    (25.0, DecisionKind.ALLOWED, ActionKind.COUPON, 300),
    (40.0, DecisionKind.NEEDS_APPROVAL, ActionKind.CANCELLATION, 4_500),
    (88.0, DecisionKind.ALLOWED, ActionKind.REFUND, 700),
    (95.0, DecisionKind.NEEDS_APPROVAL, ActionKind.REFUND, 2_000),
)
ORDER_DAYS = (0.1, 2.5, 7, 13, 26, 41, 60, 89, 91, 120)


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


def claim_record(
    order: Order, at: datetime, decision: DecisionKind, kind: ActionKind, cents: int
) -> ClaimRecord:
    return ClaimRecord(
        id=new_id(),
        order_id=order.id,
        customer_id=order.customer_id,
        claimed_at=at,
        reason=ClaimReason.ITEM_MISSING,
        action=kind,
        claimed_items=(),
        requested_amount_cents=cents,
        resolution=resolution(decision),
        is_fraud=False,
    )


async def store_as_the_application_does(
    session: AsyncSession, orders: list[Order], claims: list[ClaimRecord]
) -> None:
    await insert_orders(session, orders)
    await session.execute(
        update(CustomerRow)
        .where(CustomerRow.id == orders[0].customer_id)
        .values(created_at=CREATED_AT)
    )
    for claim in claims:
        order = next(o for o in orders if o.id == claim.order_id)
        conversation = new_id()
        await session.execute(
            insert(ConversationRow).values(
                id=conversation,
                sandbox_id=order.sandbox_id,
                customer_id=order.customer_id,
                order_id=order.id,
                status="open",
            )
        )
        proposal = {
            "kind": claim.action.value,
            "reason": claim.reason.value,
            "amount_cents": claim.requested_amount_cents,
        }
        match claim.resolution.decision:
            case DecisionKind.ALLOWED:
                values = action_values(
                    order,
                    conversation,
                    kind=claim.action.value,
                    amount_cents=claim.requested_amount_cents,
                    claimed_at=claim.claimed_at,
                    executed_at=claim.claimed_at,
                )
                await session.execute(insert(ActionRow).values(**values))
            case DecisionKind.NEEDS_APPROVAL:
                values = approval_values(
                    order,
                    conversation,
                    claimed_at=claim.claimed_at,
                    proposal=proposal,
                    **decision_values("rejected", "Sem evidência"),
                )
                await session.execute(insert(ApprovalRequestRow).values(**values))
            case DecisionKind.DENIED:
                pass  # the application stores nothing for a denied claim


def same(left: tuple[float, ...], right: tuple[float, ...]) -> bool:
    return len(left) == len(right) and all(
        a == b or (math.isnan(a) and math.isnan(b)) for a, b in zip(left, right, strict=True)
    )


async def test_the_dataset_path_and_the_postgres_path_give_the_same_vector(
    session: AsyncSession,
) -> None:
    scope = CustomerScope(SandboxId(new_id()), CustomerId(new_id()))
    orders = [
        build_order(
            sandbox_id=scope.sandbox_id,
            customer_id=scope.customer_id,
            placed_at=CLAIMED_AT - timedelta(days=days),
        )
        for days in ORDER_DAYS
    ]
    target_order = orders[0]
    priors = [
        claim_record(orders[1], CLAIMED_AT - timedelta(days=days), decision, kind, cents)
        for days, decision, kind, cents in PRIOR_CLAIMS
    ]
    target = claim_record(target_order, CLAIMED_AT, DecisionKind.ALLOWED, ActionKind.REFUND, 4_000)
    await store_as_the_application_does(session, orders, priors)

    history = await PostgresFraudContextReader(session).history(scope, CLAIMED_AT)
    proposal = ProposedAction(ActionKind.REFUND, ClaimReason.ITEM_MISSING, Money(4_000))
    serving = compute_features(claim_context(target_order, proposal, CLAIMED_AT, history))

    customer = Customer(
        id=scope.customer_id,
        sandbox_id=scope.sandbox_id,
        created_at=CREATED_AT,
        name="Cliente",
        cpf="00000000000",
        phone="+55 11 90000-0000",
        email="cliente@example.com",
        address="Rua Exemplo, 1",
    )
    table = build_claim_table(
        [*priors, target],
        [CustomerRecord.from_domain(customer)],
        [OrderRecord.from_domain(order) for order in orders],
    )
    training = tuple(float(value) for value in table.features[table.claim_ids.index(target.id)])

    assert same(serving, training), list(zip(serving, training, strict=True))
    # The history really had something to count: two claims went to review, one was denied.
    assert sum(1 for claim in history.claims if claim.reviewed) == 2
