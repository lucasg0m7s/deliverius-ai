from datetime import UTC, datetime
from typing import assert_never

import pytest

from deliverius.policy import (
    ActionKind,
    Allowed,
    ApprovalReason,
    ApprovedBy,
    ClaimedItem,
    ClaimReason,
    DenialReason,
    Denied,
    NeedsApproval,
    PolicyDecision,
    PolicyDocumentId,
    PriorAction,
    ProposedAction,
)
from deliverius.shared import InvariantError, Money, OrderId, OrderItemId, new_id

NOW = datetime(2026, 10, 1, 18, 0, tzinfo=UTC)


def test_proposal_accepts_malformed_items_so_the_engine_can_deny_them() -> None:
    proposal = ProposedAction(
        kind=ActionKind.REFUND,
        reason=ClaimReason.ITEM_MISSING,
        amount=Money(800),
        items=(ClaimedItem(OrderItemId(new_id()), quantity=-2),),
    )
    assert proposal.items[0].quantity == -2


def test_proposal_has_no_items_by_default() -> None:
    proposal = ProposedAction(ActionKind.COUPON, ClaimReason.LATE_DELIVERY, Money(1_000))
    assert proposal.items == ()


def test_prior_action_requires_an_aware_instant() -> None:
    with pytest.raises(InvariantError) as error:
        PriorAction(
            order_id=OrderId(new_id()),
            kind=ActionKind.REFUND,
            reason=ClaimReason.ITEM_MISSING,
            amount=Money(800),
            items=(),
            executed_at=datetime(2026, 10, 1, 18, 0),  # noqa: DTZ001 - the naive instant under test
            approved_by=ApprovedBy.AUTOMATIC,
        )
    assert error.value.rule == "naive_datetime.executed_at"


def test_needs_approval_requires_a_reason() -> None:
    with pytest.raises(InvariantError) as error:
        NeedsApproval(ceiling=Money(800), document=PolicyDocumentId.POL_03, reasons=())
    assert error.value.rule == "needs_approval.reasons_empty"


def test_document_ids_are_pol_01_to_pol_12() -> None:
    assert [document.value for document in PolicyDocumentId] == [
        f"POL-{number:02d}" for number in range(1, 13)
    ]


def test_reason_codes_are_stable_snake_case_identifiers() -> None:
    codes = [reason.value for reason in (*DenialReason, *ApprovalReason)]
    assert all(code.isidentifier() and code == code.lower() for code in codes)
    assert len(codes) == len(set(codes))


def describe(decision: PolicyDecision) -> str:
    """mypy fails this function if a decision variant is ever added without a case."""
    match decision:
        case Allowed():
            return "allowed"
        case NeedsApproval():
            return "needs_approval"
        case Denied():
            return "denied"
        case unreachable:
            assert_never(unreachable)


@pytest.mark.parametrize(
    ("decision", "expected"),
    [
        (Allowed(Money(800), PolicyDocumentId.POL_03), "allowed"),
        (
            NeedsApproval(
                Money(800), PolicyDocumentId.POL_03, (ApprovalReason.EXCEEDS_ACTION_LIMIT,)
            ),
            "needs_approval",
        ),
        (Denied(DenialReason.NOT_COVERED, None, None), "denied"),
    ],
)
def test_exhaustive_match_covers_the_three_decisions(
    decision: PolicyDecision, expected: str
) -> None:
    assert describe(decision) == expected
