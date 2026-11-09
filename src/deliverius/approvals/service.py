import structlog

from deliverius.approvals.models import (
    MAX_REASON_LENGTH,
    ApprovalRequest,
    ApprovalStatus,
    Decision,
    Handoff,
    HandoffStatus,
    NewApprovalRequest,
    Priority,
)
from deliverius.approvals.ports import ApprovalUnitOfWork
from deliverius.approvals.summary import HandoffSummary
from deliverius.orders import Page, PageRequest
from deliverius.policy import proposal_key
from deliverius.shared.access import AccessScope, CustomerScope, SandboxScope
from deliverius.shared.audit import AuditActorType, AuditEntry, JsonValue
from deliverius.shared.clock import Clock
from deliverius.shared.errors import ConflictError, InvalidInputError, NotFoundError
from deliverius.shared.ids import (
    ApprovalRequestId,
    ConversationId,
    HandoffId,
    ReviewerId,
    new_id,
)

logger = structlog.get_logger(__name__)

_CONFLICT_EVENTS = {
    "approval_already_pending": "approvals.pending_conflict",
    "approval_conversation_mismatch": "approvals.conversation_mismatch",
    "handoff_conversation_mismatch": "approvals.handoff_conversation_mismatch",
}


class ApprovalService:
    """Asks for a person's decision on a proposal, and records that decision once.

    The reviewer's reason is free text: it goes into the request row and nowhere else, never into a
    log or into the audit data, which is kept for years and cannot be changed.
    """

    def __init__(self, unit_of_work: ApprovalUnitOfWork, clock: Clock) -> None:
        self._unit_of_work = unit_of_work
        self._clock = clock

    async def request(self, scope: CustomerScope, new: NewApprovalRequest) -> ApprovalRequest:
        """Idempotent: the same proposal in the same conversation returns the existing request."""
        request = self._build(scope, new)
        async with self._unit_of_work() as unit:
            try:
                stored = await unit.approvals.create(scope, request)
            except ConflictError as error:
                logger.warning(
                    _CONFLICT_EVENTS[error.code],
                    conversation_id=str(new.conversation_id),
                    order_id=str(new.order_id),
                )
                raise
            if not stored.is_new:
                logger.info(
                    "approvals.request_replayed",
                    approval_id=str(stored.value.id),
                    status=stored.value.status.value,
                )
                return stored.value
            await unit.audit.record(_requested_entry(request))
        logger.info(
            "approvals.requested",
            approval_id=str(request.id),
            kind=request.proposal.kind.value,
            amount_cents=request.proposal.amount.cents,
        )
        return request

    async def get(self, scope: AccessScope, approval_id: ApprovalRequestId) -> ApprovalRequest:
        async with self._unit_of_work() as unit:
            request = await unit.approvals.get(scope, approval_id)
        if request is None:
            raise NotFoundError
        return request

    async def pending_queue(self, scope: SandboxScope, page: PageRequest) -> Page[ApprovalRequest]:
        async with self._unit_of_work() as unit:
            return await unit.approvals.pending_queue(scope, page)

    async def decide(
        self,
        scope: SandboxScope,
        approval_id: ApprovalRequestId,
        *,
        expected_version: int,
        decision: Decision,
        reason: str,
        reviewer_id: ReviewerId,
    ) -> ApprovalRequest:
        """Decide a pending request the reviewer saw at `expected_version`.

        Repeating a decision already taken returns the current state. Raises `NotFoundError`,
        `InvalidInputError` for a reason that is empty or too long, and `ConflictError` with code
        `approval_already_decided` or `approval_version_stale`.
        """
        cleaned = _clean_reason(reason)
        async with self._unit_of_work() as unit:
            decided = await unit.approvals.decide(
                scope,
                approval_id,
                expected_version=expected_version,
                decision=decision,
                reason=cleaned,
                reviewer_id=reviewer_id,
                at=self._clock.now(),
            )
            if decided is not None:
                await unit.audit.record(self._decided_entry(decided, decision, reviewer_id))
                logger.info(
                    "approvals.decided",
                    approval_id=str(approval_id),
                    decision=decision.value,
                    version=decided.version,
                )
                return decided
            current = await unit.approvals.get(scope, approval_id)
        return _explain_refusal(current, approval_id, expected_version, decision)

    def _build(self, scope: CustomerScope, new: NewApprovalRequest) -> ApprovalRequest:
        return ApprovalRequest(
            id=ApprovalRequestId(new_id()),
            sandbox_id=scope.sandbox_id,
            customer_id=scope.customer_id,
            conversation_id=new.conversation_id,
            order_id=new.order_id,
            proposal_key=proposal_key(new.conversation_id, new.order_id, new.proposal),
            proposal=new.proposal,
            claimed_at=new.claimed_at,
            policy=new.decision,
            fraud=new.fraud,
            status=ApprovalStatus.PENDING,
            version=1,
            decided_by=None,
            decided_at=None,
            decision_reason=None,
            created_at=self._clock.now(),
        )

    def _decided_entry(
        self, decided: ApprovalRequest, decision: Decision, reviewer_id: ReviewerId
    ) -> AuditEntry:
        return AuditEntry(
            sandbox_id=decided.sandbox_id,
            actor_type=AuditActorType.REVIEWER,
            actor_id=reviewer_id,
            action="approvals.decided",
            target_type="approval_request",
            target_id=decided.id,
            occurred_at=self._clock.now(),
            data={"decision": decision.value, "version": decided.version},
        )


class HandoffService:
    """Hands a conversation to a person with a short, personal-data-free summary."""

    def __init__(self, unit_of_work: ApprovalUnitOfWork, clock: Clock) -> None:
        self._unit_of_work = unit_of_work
        self._clock = clock

    async def create(
        self,
        scope: CustomerScope,
        conversation_id: ConversationId,
        priority: Priority,
        summary: HandoffSummary,
    ) -> Handoff:
        """Idempotent: a conversation has at most one open handoff, and a repeat returns it.

        Raises `InvalidInputError` when the summary holds personal data; nothing is stored.
        """
        field = summary.personal_data_field()
        if field is not None:
            logger.warning(
                "approvals.handoff_rejected_personal_data",
                conversation_id=str(conversation_id),
                field=field,
            )
            raise InvalidInputError("handoff_summary.personal_data")
        handoff = Handoff(
            id=HandoffId(new_id()),
            sandbox_id=scope.sandbox_id,
            customer_id=scope.customer_id,
            conversation_id=conversation_id,
            priority=priority,
            summary=summary,
            status=HandoffStatus.OPEN,
            assigned_to=None,
            assigned_at=None,
            created_at=self._clock.now(),
        )
        async with self._unit_of_work() as unit:
            try:
                stored = await unit.handoffs.create(scope, handoff)
            except ConflictError as error:
                logger.warning(_CONFLICT_EVENTS[error.code], conversation_id=str(conversation_id))
                raise
            if not stored.is_new:
                logger.info("approvals.handoff_replayed", handoff_id=str(stored.value.id))
                return stored.value
            await unit.audit.record(self._created_entry(handoff))
        logger.info(
            "approvals.handoff_created",
            handoff_id=str(handoff.id),
            priority=handoff.priority.value,
        )
        return handoff

    async def queue(self, scope: SandboxScope, page: PageRequest) -> Page[Handoff]:
        async with self._unit_of_work() as unit:
            return await unit.handoffs.queue(scope, page)

    async def assign(
        self, scope: SandboxScope, handoff_id: HandoffId, reviewer_id: ReviewerId
    ) -> Handoff:
        """Take an open handoff. The same reviewer taking it again gets it back as is.

        Raises `NotFoundError`, or `ConflictError` with code `handoff_already_assigned`.
        """
        async with self._unit_of_work() as unit:
            assigned = await unit.handoffs.assign(scope, handoff_id, reviewer_id, self._clock.now())
            if assigned is not None:
                await unit.audit.record(self._assigned_entry(assigned, reviewer_id))
                logger.info("approvals.handoff_assigned", handoff_id=str(handoff_id))
                return assigned
            current = await unit.handoffs.get(scope, handoff_id)
        if current is None:
            raise NotFoundError
        if current.assigned_to == reviewer_id:
            return current
        logger.warning("approvals.handoff_assign_conflict", handoff_id=str(handoff_id))
        raise ConflictError("handoff_already_assigned")

    def _created_entry(self, handoff: Handoff) -> AuditEntry:
        return AuditEntry(
            sandbox_id=handoff.sandbox_id,
            actor_type=AuditActorType.AGENT,
            actor_id=None,
            action="approvals.handoff_created",
            target_type="handoff",
            target_id=handoff.id,
            occurred_at=handoff.created_at,
            data={"priority": handoff.priority.value, "intent": handoff.summary.intent},
        )

    def _assigned_entry(self, handoff: Handoff, reviewer_id: ReviewerId) -> AuditEntry:
        return AuditEntry(
            sandbox_id=handoff.sandbox_id,
            actor_type=AuditActorType.REVIEWER,
            actor_id=reviewer_id,
            action="approvals.handoff_assigned",
            target_type="handoff",
            target_id=handoff.id,
            occurred_at=self._clock.now(),
            data={"priority": handoff.priority.value},
        )


def _clean_reason(reason: str) -> str:
    cleaned = reason.strip()
    if not cleaned:
        raise InvalidInputError("approval_decision.reason_empty")
    if len(cleaned) > MAX_REASON_LENGTH:
        raise InvalidInputError("approval_decision.reason_too_long")
    return cleaned


def _explain_refusal(
    current: ApprovalRequest | None,
    approval_id: ApprovalRequestId,
    expected_version: int,
    decision: Decision,
) -> ApprovalRequest:
    """The conditional write matched nothing: read the request again to say why."""
    if current is None:
        raise NotFoundError
    if current.status is decision.status:
        logger.info(
            "approvals.decision_replayed",
            approval_id=str(approval_id),
            version=current.version,
        )
        return current
    if current.status is not ApprovalStatus.PENDING:
        logger.warning(
            "approvals.decision_conflict",
            approval_id=str(approval_id),
            version=current.version,
        )
        raise ConflictError("approval_already_decided")
    logger.warning(
        "approvals.version_stale",
        approval_id=str(approval_id),
        expected_version=expected_version,
        version=current.version,
    )
    raise ConflictError("approval_version_stale")


def _requested_entry(request: ApprovalRequest) -> AuditEntry:
    data: dict[str, JsonValue] = {
        "kind": request.proposal.kind.value,
        "reason": request.proposal.reason.value,
        "amount_cents": request.proposal.amount.cents,
        "policy_reasons": [reason.value for reason in request.policy.reasons],
    }
    return AuditEntry(
        sandbox_id=request.sandbox_id,
        actor_type=AuditActorType.AGENT,
        actor_id=None,
        action="approvals.requested",
        target_type="approval_request",
        target_id=request.id,
        occurred_at=request.created_at,
        data=data,
    )
