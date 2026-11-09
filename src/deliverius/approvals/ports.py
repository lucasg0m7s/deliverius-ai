from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Protocol

from deliverius.approvals.models import (
    ApprovalRequest,
    Decision,
    Handoff,
    Stored,
)
from deliverius.orders import Page, PageRequest
from deliverius.shared.access import AccessScope, CustomerScope, SandboxScope
from deliverius.shared.audit import AuditSink
from deliverius.shared.ids import ApprovalRequestId, HandoffId, ReviewerId


class ApprovalStore(Protocol):
    """Approval requests and the conversation status that follows them. Scope on every method."""

    async def create(
        self, scope: CustomerScope, request: ApprovalRequest
    ) -> Stored[ApprovalRequest]:
        """Store the request and move the conversation to `awaiting_approval`.

        If the conversation already has this proposal, return that one, in any status. Raises
        `ConflictError` with code `approval_already_pending` when another proposal is pending, and
        `approval_conversation_mismatch` when the conversation is not the customer's for this
        order.
        """
        ...

    async def get(
        self, scope: AccessScope, approval_id: ApprovalRequestId
    ) -> ApprovalRequest | None: ...

    async def pending_queue(self, scope: SandboxScope, page: PageRequest) -> Page[ApprovalRequest]:
        """Oldest first. Raises `InvalidInputError` for a bad cursor."""
        ...

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
        """Decide in one conditional write and reopen the conversation.

        `None` when the request is missing, outside the scope, no longer pending or at another
        version: the caller reads it again to tell which.
        """
        ...


class HandoffStore(Protocol):
    async def create(self, scope: CustomerScope, handoff: Handoff) -> Stored[Handoff]:
        """Store the handoff and move the conversation to `transferred`.

        If the conversation already has an open one, return that. Raises `ConflictError` with code
        `handoff_conversation_mismatch` when the conversation is not the customer's.
        """
        ...

    async def get(self, scope: SandboxScope, handoff_id: HandoffId) -> Handoff | None: ...

    async def queue(self, scope: SandboxScope, page: PageRequest) -> Page[Handoff]:
        """High priority first, then oldest first. Raises `InvalidInputError` for a bad cursor."""
        ...

    async def assign(
        self, scope: SandboxScope, handoff_id: HandoffId, reviewer_id: ReviewerId, at: datetime
    ) -> Handoff | None:
        """Assign in one conditional write; `None` when missing, outside the scope or not open."""
        ...


class ApprovalUnit(Protocol):
    """What one approvals transaction touches; all of it commits or rolls back together."""

    @property
    def approvals(self) -> ApprovalStore: ...

    @property
    def handoffs(self) -> HandoffStore: ...

    @property
    def audit(self) -> AuditSink: ...


type ApprovalUnitOfWork = Callable[[], AbstractAsyncContextManager[ApprovalUnit]]
