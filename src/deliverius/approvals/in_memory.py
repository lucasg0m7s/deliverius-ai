from dataclasses import replace
from datetime import datetime
from types import TracebackType
from typing import Self

from deliverius.approvals.cursor import (
    approval_cursor,
    decode_approval_cursor,
    decode_handoff_cursor,
    handoff_cursor,
    page_of,
)
from deliverius.approvals.models import (
    ApprovalRequest,
    ApprovalStatus,
    ConversationStatus,
    Decision,
    Handoff,
    HandoffStatus,
    Stored,
)
from deliverius.orders import Page, PageRequest
from deliverius.shared.access import AccessScope, CustomerScope, SandboxScope, is_visible
from deliverius.shared.audit import InMemoryAuditSink
from deliverius.shared.errors import ConflictError
from deliverius.shared.ids import (
    ApprovalRequestId,
    ConversationId,
    HandoffId,
    ReviewerId,
)


class InMemoryConversations:
    """The conversation statuses the stores move, for tests to read."""

    def __init__(self) -> None:
        self.statuses: dict[ConversationId, ConversationStatus] = {}

    def add(self, conversation_id: ConversationId) -> None:
        self.statuses[conversation_id] = ConversationStatus.OPEN

    def move(
        self,
        conversation_id: ConversationId,
        source: ConversationStatus,
        target: ConversationStatus,
    ) -> None:
        if self.statuses.get(conversation_id) is source:
            self.statuses[conversation_id] = target

    def transfer(self, conversation_id: ConversationId) -> None:
        if self.statuses.get(conversation_id) not in (None, ConversationStatus.CLOSED):
            self.statuses[conversation_id] = ConversationStatus.TRANSFERRED


class InMemoryApprovalStore:
    """`ApprovalStore` kept in a dict, with the same contract as Postgres.

    The foreign keys of the database are not simulated: the stores trust the conversation they get.
    """

    def __init__(self, conversations: InMemoryConversations) -> None:
        self._conversations = conversations
        self._requests: dict[ApprovalRequestId, ApprovalRequest] = {}

    async def create(
        self, scope: CustomerScope, request: ApprovalRequest
    ) -> Stored[ApprovalRequest]:
        same_proposal = next(
            (
                stored
                for stored in self._requests.values()
                if stored.conversation_id == request.conversation_id
                and stored.proposal_key == request.proposal_key
            ),
            None,
        )
        if same_proposal is not None:
            if not is_visible(scope, same_proposal.sandbox_id, same_proposal.customer_id):
                raise ConflictError("approval_conversation_mismatch")
            return Stored(same_proposal, is_new=False)
        if any(
            stored.conversation_id == request.conversation_id
            and stored.status is ApprovalStatus.PENDING
            for stored in self._requests.values()
        ):
            raise ConflictError("approval_already_pending")
        self._requests[request.id] = request
        self._conversations.move(
            request.conversation_id,
            ConversationStatus.OPEN,
            ConversationStatus.AWAITING_APPROVAL,
        )
        return Stored(request, is_new=True)

    async def get(
        self, scope: AccessScope, approval_id: ApprovalRequestId
    ) -> ApprovalRequest | None:
        request = self._requests.get(approval_id)
        if request is None or not is_visible(scope, request.sandbox_id, request.customer_id):
            return None
        return request

    async def pending_queue(self, scope: SandboxScope, page: PageRequest) -> Page[ApprovalRequest]:
        after = decode_approval_cursor(page.cursor) if page.cursor is not None else None
        pending = sorted(
            (
                request
                for request in self._requests.values()
                if request.sandbox_id == scope.sandbox_id
                and request.status is ApprovalStatus.PENDING
            ),
            key=lambda request: (request.created_at, request.id),
        )
        if after is not None:
            pending = [
                request
                for request in pending
                if (request.created_at, request.id) > (after.created_at, after.id)
            ]
        return page_of(
            pending[: page.limit + 1],
            page.limit,
            lambda last: approval_cursor(last.created_at, last.id),
        )

    async def decide(
        self,
        scope: SandboxScope,
        approval_id: ApprovalRequestId,
        *,
        expected_version: int,
        decision: Decision,
        reason: str,
        reviewer_id: ReviewerId,
        at: datetime,
    ) -> ApprovalRequest | None:
        request = await self.get(scope, approval_id)
        if (
            request is None
            or request.status is not ApprovalStatus.PENDING
            or request.version != expected_version
        ):
            return None
        decided = replace(
            request,
            status=decision.status,
            version=request.version + 1,
            decided_by=reviewer_id,
            decided_at=at,
            decision_reason=reason,
        )
        self._requests[approval_id] = decided
        self._conversations.move(
            request.conversation_id,
            ConversationStatus.AWAITING_APPROVAL,
            ConversationStatus.OPEN,
        )
        return decided


class InMemoryHandoffStore:
    """`HandoffStore` kept in a dict, with the same contract as Postgres."""

    def __init__(self, conversations: InMemoryConversations) -> None:
        self._conversations = conversations
        self._handoffs: dict[HandoffId, Handoff] = {}

    async def create(self, scope: CustomerScope, handoff: Handoff) -> Stored[Handoff]:
        open_one = next(
            (
                stored
                for stored in self._handoffs.values()
                if stored.conversation_id == handoff.conversation_id
                and stored.status is HandoffStatus.OPEN
            ),
            None,
        )
        if open_one is not None:
            if not is_visible(scope, open_one.sandbox_id, open_one.customer_id):
                raise ConflictError("handoff_conversation_mismatch")
            return Stored(open_one, is_new=False)
        self._handoffs[handoff.id] = handoff
        self._conversations.transfer(handoff.conversation_id)
        return Stored(handoff, is_new=True)

    async def get(self, scope: SandboxScope, handoff_id: HandoffId) -> Handoff | None:
        handoff = self._handoffs.get(handoff_id)
        return handoff if handoff is not None and handoff.sandbox_id == scope.sandbox_id else None

    async def queue(self, scope: SandboxScope, page: PageRequest) -> Page[Handoff]:
        after = decode_handoff_cursor(page.cursor) if page.cursor is not None else None
        open_ones = sorted(
            (
                handoff
                for handoff in self._handoffs.values()
                if handoff.sandbox_id == scope.sandbox_id and handoff.status is HandoffStatus.OPEN
            ),
            key=lambda handoff: (-handoff.priority.rank, handoff.created_at, handoff.id),
        )
        if after is not None:
            open_ones = [
                handoff
                for handoff in open_ones
                if (-handoff.priority.rank, handoff.created_at, handoff.id)
                > (-after.priority_rank, after.created_at, after.id)
            ]
        return page_of(
            open_ones[: page.limit + 1],
            page.limit,
            lambda last: handoff_cursor(last.priority.rank, last.created_at, last.id),
        )

    async def assign(
        self, scope: SandboxScope, handoff_id: HandoffId, reviewer_id: ReviewerId, at: datetime
    ) -> Handoff | None:
        handoff = await self.get(scope, handoff_id)
        if handoff is None or handoff.status is not HandoffStatus.OPEN:
            return None
        assigned = replace(
            handoff, status=HandoffStatus.ASSIGNED, assigned_to=reviewer_id, assigned_at=at
        )
        self._handoffs[handoff_id] = assigned
        return assigned


class InMemoryApprovalUnitOfWork:
    """Both stores and the audit in memory; the unit of work and its own transaction at once.

    There is no rollback: atomicity is proved against Postgres.
    """

    def __init__(self) -> None:
        self.conversations = InMemoryConversations()
        self.approvals = InMemoryApprovalStore(self.conversations)
        self.handoffs = InMemoryHandoffStore(self.conversations)
        self.audit = InMemoryAuditSink()

    def __call__(self) -> Self:
        return self

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None
