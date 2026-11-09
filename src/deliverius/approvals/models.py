"""Approval requests and handoffs, and the statuses of their tables and of the conversation.

The database keeps literal copies of the status enums (it cannot import them); a test keeps both
equal.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final

from deliverius.approvals.summary import HandoffSummary
from deliverius.policy import NeedsApproval, ProposedAction
from deliverius.shared.clock import require_aware
from deliverius.shared.errors import InvariantError
from deliverius.shared.ids import (
    ApprovalRequestId,
    ConversationId,
    CustomerId,
    HandoffId,
    OrderId,
    ReviewerId,
    SandboxId,
)

MAX_REASON_LENGTH: Final = 500


class ConversationStatus(StrEnum):
    OPEN = "open"
    AWAITING_APPROVAL = "awaiting_approval"
    TRANSFERRED = "transferred"
    CLOSED = "closed"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class Decision(StrEnum):
    """What a reviewer decides; the values are the statuses the request moves to."""

    APPROVED = "approved"
    REJECTED = "rejected"

    @property
    def status(self) -> ApprovalStatus:
        return ApprovalStatus(self.value)


class HandoffStatus(StrEnum):
    OPEN = "open"
    ASSIGNED = "assigned"


class Priority(StrEnum):
    NORMAL = "normal"
    HIGH = "high"

    @property
    def rank(self) -> int:
        """What the queue sorts by, as the database computes it for `priority_rank`."""
        return 2 if self is Priority.HIGH else 1


@dataclass(frozen=True, slots=True)
class FraudFactor:
    feature: str
    contribution: float


@dataclass(frozen=True, slots=True)
class FraudAssessment:
    """The anti-fraud score the reviewer sees; filled by the agent once the model exists."""

    score: float
    model_version: str
    factors: tuple[FraudFactor, ...]

    def __post_init__(self) -> None:
        if not 0.0 <= self.score <= 1.0:
            raise InvariantError("fraud_assessment.score_out_of_range")


@dataclass(frozen=True, slots=True)
class NewApprovalRequest:
    """What the agent hands over when the policy sent a proposal to a person."""

    conversation_id: ConversationId
    order_id: OrderId
    proposal: ProposedAction
    claimed_at: datetime
    decision: NeedsApproval
    fraud: FraudAssessment | None = None

    def __post_init__(self) -> None:
        require_aware(self.claimed_at, "claimed_at")


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    id: ApprovalRequestId
    sandbox_id: SandboxId
    customer_id: CustomerId
    conversation_id: ConversationId
    order_id: OrderId
    proposal_key: str
    proposal: ProposedAction
    claimed_at: datetime
    policy: NeedsApproval
    fraud: FraudAssessment | None
    status: ApprovalStatus
    version: int
    decided_by: ReviewerId | None
    decided_at: datetime | None
    # Free text from the reviewer: it never goes to a log or to the audit data.
    decision_reason: str | None
    created_at: datetime

    def __post_init__(self) -> None:
        require_aware(self.claimed_at, "claimed_at")
        require_aware(self.created_at, "created_at")
        if self.version < 1:
            raise InvariantError("approval_request.version_below_one")
        decision = (self.decided_by, self.decided_at, self.decision_reason)
        if self.status is ApprovalStatus.PENDING:
            consistent = all(value is None for value in decision)
        else:
            consistent = all(value is not None for value in decision)
        if not consistent:
            raise InvariantError("approval_request.decision_inconsistent")
        if self.decided_at is not None:
            require_aware(self.decided_at, "decided_at")


@dataclass(frozen=True, slots=True)
class Handoff:
    id: HandoffId
    sandbox_id: SandboxId
    customer_id: CustomerId
    conversation_id: ConversationId
    priority: Priority
    summary: HandoffSummary
    status: HandoffStatus
    assigned_to: ReviewerId | None
    assigned_at: datetime | None
    created_at: datetime

    def __post_init__(self) -> None:
        require_aware(self.created_at, "created_at")
        assignment = (self.assigned_to, self.assigned_at)
        if self.status is HandoffStatus.ASSIGNED:
            consistent = all(value is not None for value in assignment)
        else:
            consistent = all(value is None for value in assignment)
        if not consistent:
            raise InvariantError("handoff.assignment_inconsistent")
        if self.assigned_at is not None:
            require_aware(self.assigned_at, "assigned_at")


@dataclass(frozen=True, slots=True)
class Stored[T]:
    """A row the store wrote or found: `is_new` tells a creation from a repetition."""

    value: T
    is_new: bool
