"""What the executor receives (a request with its authority) and what it answers."""

from dataclasses import dataclass
from datetime import datetime

from deliverius.policy import ApprovedBy, Denied, NeedsApproval, PriorAction, ProposedAction
from deliverius.shared.clock import require_aware
from deliverius.shared.errors import InvariantError
from deliverius.shared.ids import (
    ActionId,
    ApprovalRequestId,
    ConversationId,
    CustomerId,
    OrderId,
    ReviewerId,
    SandboxId,
)


@dataclass(frozen=True, slots=True)
class Automatic:
    """The policy alone allows the action: no person was involved."""


@dataclass(frozen=True, slots=True)
class ReviewerApproved:
    """A reviewer approved this proposal; the database checks it is the right approval."""

    approval_request_id: ApprovalRequestId
    reviewer_id: ReviewerId


type Authority = Automatic | ReviewerApproved


@dataclass(frozen=True, slots=True)
class ExecutionRequest:
    """`claimed_at` is when the customer complained: the policy deadlines count from it."""

    conversation_id: ConversationId
    order_id: OrderId
    proposal: ProposedAction
    claimed_at: datetime
    authority: Authority

    def __post_init__(self) -> None:
        require_aware(self.claimed_at, "claimed_at")


@dataclass(frozen=True, slots=True)
class ExecutedAction:
    id: ActionId
    sandbox_id: SandboxId
    customer_id: CustomerId
    order_id: OrderId
    conversation_id: ConversationId
    proposal: ProposedAction
    idempotency_key: str
    approved_by: ApprovedBy
    approval_request_id: ApprovalRequestId | None
    claimed_at: datetime
    executed_at: datetime

    def __post_init__(self) -> None:
        require_aware(self.claimed_at, "claimed_at")
        require_aware(self.executed_at, "executed_at")
        if (self.approved_by is ApprovedBy.REVIEWER) != (self.approval_request_id is not None):
            raise InvariantError("executed_action.approval_inconsistent")

    def as_prior(self) -> PriorAction:
        return PriorAction(
            order_id=self.order_id,
            kind=self.proposal.kind,
            reason=self.proposal.reason,
            amount=self.proposal.amount,
            items=self.proposal.items,
            executed_at=self.executed_at,
            approved_by=self.approved_by,
        )


@dataclass(frozen=True, slots=True)
class Executed:
    """The action is stored; `replayed` says an earlier execution of the same proposal made it."""

    action: ExecutedAction
    replayed: bool


@dataclass(frozen=True, slots=True)
class NotExecuted:
    """Nothing was written: the policy decision, as re-evaluated under the locks, says why."""

    decision: Denied | NeedsApproval


type ExecutionResult = Executed | NotExecuted
