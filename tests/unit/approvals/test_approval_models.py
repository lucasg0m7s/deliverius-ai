from dataclasses import replace
from datetime import datetime
from uuid import uuid4

import pytest
from support.action_builders import delivered_order, new_conversation_id
from support.approval_builders import (
    DECIDED_AT,
    REQUESTED_AT,
    big_refund,
    fraud_assessment,
    needs_approval,
    summary,
)

from deliverius.approvals import (
    ApprovalRequest,
    ApprovalStatus,
    Decision,
    Handoff,
    HandoffStatus,
    HandoffSummary,
    NewApprovalRequest,
    Priority,
)
from deliverius.approvals.cursor import (
    MAX_CURSOR_LENGTH,
    approval_cursor,
    decode_approval_cursor,
    decode_handoff_cursor,
    handoff_cursor,
)
from deliverius.approvals.summary import (
    MAX_ACTION_TAKEN_LENGTH,
    MAX_FACT_LENGTH,
    MAX_FACTS,
)
from deliverius.shared import (
    ApprovalRequestId,
    HandoffId,
    InvalidInputError,
    InvariantError,
    ReviewerId,
    new_id,
)

NAIVE = datetime(2026, 1, 1)  # noqa: DTZ001 - the point of the test


def pending_request() -> ApprovalRequest:
    order = delivered_order()
    return ApprovalRequest(
        id=ApprovalRequestId(new_id()),
        sandbox_id=order.sandbox_id,
        customer_id=order.customer_id,
        conversation_id=new_conversation_id(),
        order_id=order.id,
        proposal_key="k",
        proposal=big_refund(order),
        claimed_at=REQUESTED_AT,
        policy=needs_approval(),
        fraud=None,
        status=ApprovalStatus.PENDING,
        version=1,
        decided_by=None,
        decided_at=None,
        decision_reason=None,
        created_at=REQUESTED_AT,
    )


def open_handoff() -> Handoff:
    order = delivered_order()
    return Handoff(
        id=HandoffId(new_id()),
        sandbox_id=order.sandbox_id,
        customer_id=order.customer_id,
        conversation_id=new_conversation_id(),
        priority=Priority.HIGH,
        summary=summary(),
        status=HandoffStatus.OPEN,
        assigned_to=None,
        assigned_at=None,
        created_at=REQUESTED_AT,
    )


class TestApprovalRequest:
    def test_decided_request_carries_the_whole_decision(self) -> None:
        decided = replace(
            pending_request(),
            status=ApprovalStatus.APPROVED,
            version=2,
            decided_by=ReviewerId(new_id()),
            decided_at=DECIDED_AT,
            decision_reason="ok",
        )

        assert decided.status is Decision.APPROVED.status

    @pytest.mark.parametrize(
        "changes",
        [
            {"decided_by": ReviewerId(new_id())},
            {"status": ApprovalStatus.REJECTED, "version": 2},
            {
                "status": ApprovalStatus.APPROVED,
                "decided_by": ReviewerId(new_id()),
                "decided_at": DECIDED_AT,
            },
        ],
    )
    def test_partial_decision_is_refused(self, changes: dict[str, object]) -> None:
        with pytest.raises(InvariantError) as raised:
            replace(pending_request(), **changes)  # type: ignore[arg-type]

        assert raised.value.rule == "approval_request.decision_inconsistent"

    def test_version_starts_at_one(self) -> None:
        with pytest.raises(InvariantError) as raised:
            replace(pending_request(), version=0)

        assert raised.value.rule == "approval_request.version_below_one"

    @pytest.mark.parametrize("field", ["claimed_at", "created_at"])
    def test_naive_times_are_refused(self, field: str) -> None:
        with pytest.raises(InvariantError):
            replace(pending_request(), **{field: NAIVE})  # type: ignore[arg-type]

    def test_naive_decision_time_is_refused(self) -> None:
        with pytest.raises(InvariantError):
            replace(
                pending_request(),
                status=ApprovalStatus.APPROVED,
                decided_by=ReviewerId(new_id()),
                decided_at=NAIVE,
                decision_reason="ok",
            )

    def test_new_request_needs_an_aware_claim_time(self) -> None:
        order = delivered_order()
        with pytest.raises(InvariantError):
            NewApprovalRequest(
                conversation_id=new_conversation_id(),
                order_id=order.id,
                proposal=big_refund(order),
                claimed_at=NAIVE,
                decision=needs_approval(),
            )


class TestFraudAssessment:
    @pytest.mark.parametrize("score", [0.0, 1.0])
    def test_bounds_are_accepted(self, score: float) -> None:
        assert fraud_assessment(score).score == score

    @pytest.mark.parametrize("score", [-0.01, 1.01])
    def test_score_out_of_range_is_refused(self, score: float) -> None:
        with pytest.raises(InvariantError) as raised:
            fraud_assessment(score)

        assert raised.value.rule == "fraud_assessment.score_out_of_range"


class TestHandoff:
    def test_assigned_needs_reviewer_and_time(self) -> None:
        with pytest.raises(InvariantError) as raised:
            replace(open_handoff(), status=HandoffStatus.ASSIGNED)

        assert raised.value.rule == "handoff.assignment_inconsistent"

    def test_open_cannot_have_a_reviewer(self) -> None:
        with pytest.raises(InvariantError):
            replace(open_handoff(), assigned_to=ReviewerId(new_id()))

    def test_naive_assignment_time_is_refused(self) -> None:
        with pytest.raises(InvariantError):
            replace(
                open_handoff(),
                status=HandoffStatus.ASSIGNED,
                assigned_to=ReviewerId(new_id()),
                assigned_at=NAIVE,
            )

    def test_priority_rank_puts_high_first(self) -> None:
        assert Priority.HIGH.rank > Priority.NORMAL.rank


class TestHandoffSummary:
    @pytest.mark.parametrize("intent", ["", "Allergy", "allergy!", "a" * 41, "alergia 1"])
    def test_intent_must_be_a_short_code(self, intent: str) -> None:
        with pytest.raises(InvalidInputError) as raised:
            HandoffSummary(intent=intent)

        assert str(raised.value) == "handoff_summary.intent_format"

    def test_limits_are_accepted(self) -> None:
        handoff_summary = HandoffSummary(
            intent="a" * 40,
            facts=("x" * MAX_FACT_LENGTH,) * MAX_FACTS,
            actions_taken=("y" * MAX_ACTION_TAKEN_LENGTH,),
        )

        assert handoff_summary.personal_data_field() is None

    @pytest.mark.parametrize(
        ("changes", "code"),
        [
            ({"facts": ("x",) * (MAX_FACTS + 1)}, "handoff_summary.facts_too_many"),
            ({"facts": ("x" * (MAX_FACT_LENGTH + 1),)}, "handoff_summary.facts_too_long"),
            (
                {"actions_taken": ("y" * (MAX_ACTION_TAKEN_LENGTH + 1),)},
                "handoff_summary.actions_taken_too_long",
            ),
            ({"actions_taken": ("y",) * 11}, "handoff_summary.actions_taken_too_many"),
        ],
    )
    def test_over_the_limits_is_refused(self, changes: dict[str, object], code: str) -> None:
        with pytest.raises(InvalidInputError) as raised:
            HandoffSummary(intent="allergy", **changes)  # type: ignore[arg-type]

        assert str(raised.value) == code


class TestCursor:
    def test_round_trips(self) -> None:
        approval_id, handoff_id = uuid4(), uuid4()

        approval = decode_approval_cursor(approval_cursor(REQUESTED_AT, approval_id))
        handoff = decode_handoff_cursor(handoff_cursor(2, REQUESTED_AT, handoff_id))

        assert (approval.created_at, approval.id) == (REQUESTED_AT, approval_id)
        assert (handoff.priority_rank, handoff.created_at, handoff.id) == (
            2,
            REQUESTED_AT,
            handoff_id,
        )

    @pytest.mark.parametrize(
        "cursor",
        ["", "***", "bm9wZQ", "x" * (MAX_CURSOR_LENGTH + 1)],
    )
    def test_malformed_is_invalid_input(self, cursor: str) -> None:
        with pytest.raises(InvalidInputError):
            decode_approval_cursor(cursor)

    def test_cursor_of_the_other_queue_is_invalid_input(self) -> None:
        with pytest.raises(InvalidInputError):
            decode_handoff_cursor(approval_cursor(REQUESTED_AT, uuid4()))
