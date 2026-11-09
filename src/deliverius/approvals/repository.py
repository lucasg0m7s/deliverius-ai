from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, and_, or_, select, text, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
    FraudAssessment,
    FraudFactor,
    Handoff,
    HandoffStatus,
    Priority,
    Stored,
)
from deliverius.approvals.ports import ApprovalStore, ApprovalUnit, ApprovalUnitOfWork, HandoffStore
from deliverius.approvals.summary import HandoffSummary
from deliverius.db.audit import PostgresAuditSink
from deliverius.db.errors import translate_db_errors, violated_constraint
from deliverius.db.tables import ApprovalRequestRow, ConversationRow, HandoffRow
from deliverius.orders import Page, PageRequest
from deliverius.policy import (
    ActionKind,
    ApprovalReason,
    ClaimedItem,
    ClaimReason,
    NeedsApproval,
    PolicyDocumentId,
    ProposedAction,
)
from deliverius.shared.access import AccessScope, CustomerScope, SandboxScope
from deliverius.shared.audit import AuditSink
from deliverius.shared.errors import ConflictError
from deliverius.shared.ids import (
    ApprovalRequestId,
    ConversationId,
    CustomerId,
    HandoffId,
    OrderId,
    OrderItemId,
    ReviewerId,
    SandboxId,
)
from deliverius.shared.money import Money

PENDING_CONSTRAINT = "uq_approval_requests_pending_conversation"
APPROVAL_CONVERSATION_CONSTRAINT = "fk_approval_requests_conversation_matches_customer_order"
HANDOFF_CONVERSATION_CONSTRAINT = "fk_handoffs_conversation_matches_customer"


class PostgresApprovalStore:
    """`ApprovalStore` on Postgres. Uses the caller's session and never commits."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self, scope: CustomerScope, request: ApprovalRequest
    ) -> Stored[ApprovalRequest]:
        statement = (
            pg_insert(ApprovalRequestRow)
            .values(**_approval_values(request))
            .on_conflict_do_nothing(index_elements=["conversation_id", "proposal_key"])
            .returning(ApprovalRequestRow.id)
        )
        with translate_db_errors():
            try:
                # A savepoint: a refused row must not abort the caller's transaction.
                async with self._session.begin_nested():
                    inserted = (await self._session.execute(statement)).scalar_one_or_none()
            except IntegrityError as error:
                conflict = _approval_conflict(error)
                if conflict is None:
                    raise
                raise conflict from error
            if inserted is None:
                return Stored(await self._existing(scope, request), is_new=False)
            await _move_conversation(
                self._session,
                request.conversation_id,
                ConversationStatus.OPEN,
                ConversationStatus.AWAITING_APPROVAL,
            )
        return Stored(request, is_new=True)

    async def get(
        self, scope: AccessScope, approval_id: ApprovalRequestId
    ) -> ApprovalRequest | None:
        statement = select(ApprovalRequestRow).where(
            ApprovalRequestRow.id == approval_id, *_approval_scope(scope)
        )
        with translate_db_errors():
            row = (await self._session.execute(statement)).scalar_one_or_none()
        return None if row is None else _to_approval(row)

    async def pending_queue(self, scope: SandboxScope, page: PageRequest) -> Page[ApprovalRequest]:
        after = decode_approval_cursor(page.cursor) if page.cursor is not None else None
        statement = (
            select(ApprovalRequestRow)
            .where(
                ApprovalRequestRow.sandbox_id == scope.sandbox_id,
                ApprovalRequestRow.status == ApprovalStatus.PENDING.value,
            )
            .order_by(ApprovalRequestRow.created_at, ApprovalRequestRow.id)
            .limit(page.limit + 1)
        )
        if after is not None:
            statement = statement.where(
                tuple_(ApprovalRequestRow.created_at, ApprovalRequestRow.id)
                > tuple_(after.created_at, after.id)
            )
        with translate_db_errors():
            rows = (await self._session.execute(statement)).scalars().all()
        requests = [_to_approval(row) for row in rows]
        return page_of(requests, page.limit, lambda last: approval_cursor(last.created_at, last.id))

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
        statement = (
            update(ApprovalRequestRow)
            .where(
                ApprovalRequestRow.id == approval_id,
                ApprovalRequestRow.sandbox_id == scope.sandbox_id,
                ApprovalRequestRow.status == ApprovalStatus.PENDING.value,
                ApprovalRequestRow.version == expected_version,
            )
            .values(
                status=decision.status.value,
                decided_by=reviewer_id,
                decided_at=at,
                decision_reason=reason,
                version=ApprovalRequestRow.version + 1,
            )
            .returning(ApprovalRequestRow)
            .execution_options(populate_existing=True, synchronize_session=False)
        )
        with translate_db_errors():
            row = (await self._session.execute(statement)).scalar_one_or_none()
            if row is None:
                return None
            await _move_conversation(
                self._session,
                ConversationId(row.conversation_id),
                ConversationStatus.AWAITING_APPROVAL,
                ConversationStatus.OPEN,
            )
        return _to_approval(row)

    async def _existing(self, scope: CustomerScope, request: ApprovalRequest) -> ApprovalRequest:
        statement = select(ApprovalRequestRow).where(
            ApprovalRequestRow.conversation_id == request.conversation_id,
            ApprovalRequestRow.proposal_key == request.proposal_key,
            *_approval_scope(scope),
        )
        row = (await self._session.execute(statement)).scalar_one_or_none()
        if row is None:
            # The proposal exists, but in a conversation that is not this customer's.
            raise ConflictError("approval_conversation_mismatch")
        return _to_approval(row)


class PostgresHandoffStore:
    """`HandoffStore` on Postgres. Uses the caller's session and never commits."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, scope: CustomerScope, handoff: Handoff) -> Stored[Handoff]:
        statement = (
            pg_insert(HandoffRow)
            .values(**_handoff_values(handoff))
            .on_conflict_do_nothing(
                index_elements=["conversation_id"], index_where=text("status = 'open'")
            )
            .returning(HandoffRow.id)
        )
        with translate_db_errors():
            try:
                async with self._session.begin_nested():
                    inserted = (await self._session.execute(statement)).scalar_one_or_none()
            except IntegrityError as error:
                if violated_constraint(error) != HANDOFF_CONVERSATION_CONSTRAINT:
                    raise
                raise ConflictError("handoff_conversation_mismatch") from error
            if inserted is None:
                return Stored(await self._open_one(scope, handoff.conversation_id), is_new=False)
            await self._session.execute(
                update(ConversationRow)
                .where(
                    ConversationRow.id == handoff.conversation_id,
                    ConversationRow.status != ConversationStatus.CLOSED.value,
                )
                .values(status=ConversationStatus.TRANSFERRED.value)
            )
        return Stored(handoff, is_new=True)

    async def get(self, scope: SandboxScope, handoff_id: HandoffId) -> Handoff | None:
        statement = select(HandoffRow).where(
            HandoffRow.id == handoff_id, HandoffRow.sandbox_id == scope.sandbox_id
        )
        with translate_db_errors():
            row = (await self._session.execute(statement)).scalar_one_or_none()
        return None if row is None else _to_handoff(row)

    async def queue(self, scope: SandboxScope, page: PageRequest) -> Page[Handoff]:
        after = decode_handoff_cursor(page.cursor) if page.cursor is not None else None
        statement = (
            select(HandoffRow)
            .where(
                HandoffRow.sandbox_id == scope.sandbox_id,
                HandoffRow.status == HandoffStatus.OPEN.value,
            )
            .order_by(HandoffRow.priority_rank.desc(), HandoffRow.created_at, HandoffRow.id)
            .limit(page.limit + 1)
        )
        if after is not None:
            statement = statement.where(
                or_(
                    HandoffRow.priority_rank < after.priority_rank,
                    and_(
                        HandoffRow.priority_rank == after.priority_rank,
                        tuple_(HandoffRow.created_at, HandoffRow.id)
                        > tuple_(after.created_at, after.id),
                    ),
                )
            )
        with translate_db_errors():
            rows = (await self._session.execute(statement)).scalars().all()
        handoffs = [_to_handoff(row) for row in rows]
        return page_of(
            handoffs,
            page.limit,
            lambda last: handoff_cursor(last.priority.rank, last.created_at, last.id),
        )

    async def assign(
        self, scope: SandboxScope, handoff_id: HandoffId, reviewer_id: ReviewerId, at: datetime
    ) -> Handoff | None:
        statement = (
            update(HandoffRow)
            .where(
                HandoffRow.id == handoff_id,
                HandoffRow.sandbox_id == scope.sandbox_id,
                HandoffRow.status == HandoffStatus.OPEN.value,
            )
            .values(status=HandoffStatus.ASSIGNED.value, assigned_to=reviewer_id, assigned_at=at)
            .returning(HandoffRow)
            .execution_options(populate_existing=True, synchronize_session=False)
        )
        with translate_db_errors():
            row = (await self._session.execute(statement)).scalar_one_or_none()
        return None if row is None else _to_handoff(row)

    async def _open_one(self, scope: CustomerScope, conversation_id: ConversationId) -> Handoff:
        statement = select(HandoffRow).where(
            HandoffRow.conversation_id == conversation_id,
            HandoffRow.status == HandoffStatus.OPEN.value,
            HandoffRow.sandbox_id == scope.sandbox_id,
            HandoffRow.customer_id == scope.customer_id,
        )
        row = (await self._session.execute(statement)).scalar_one_or_none()
        if row is None:
            # The open handoff exists, but in a conversation that is not this customer's.
            raise ConflictError("handoff_conversation_mismatch")
        return _to_handoff(row)


class PostgresApprovalUnit:
    """The repositories of one transaction, on one session."""

    def __init__(self, session: AsyncSession) -> None:
        self._approvals = PostgresApprovalStore(session)
        self._handoffs = PostgresHandoffStore(session)
        self._audit = PostgresAuditSink(session)

    @property
    def approvals(self) -> ApprovalStore:
        return self._approvals

    @property
    def handoffs(self) -> HandoffStore:
        return self._handoffs

    @property
    def audit(self) -> AuditSink:
        return self._audit


def postgres_unit_of_work(factory: async_sessionmaker[AsyncSession]) -> ApprovalUnitOfWork:
    """One transaction per operation: committed on exit, rolled back on any error."""

    @asynccontextmanager
    async def open_unit() -> AsyncIterator[ApprovalUnit]:
        with translate_db_errors():
            async with factory() as session, session.begin():
                yield PostgresApprovalUnit(session)

    return open_unit


async def _move_conversation(
    session: AsyncSession,
    conversation_id: ConversationId,
    source: ConversationStatus,
    target: ConversationStatus,
) -> None:
    await session.execute(
        update(ConversationRow)
        .where(ConversationRow.id == conversation_id, ConversationRow.status == source.value)
        .values(status=target.value)
    )


def _approval_conflict(error: IntegrityError) -> ConflictError | None:
    constraint = violated_constraint(error)
    if constraint == PENDING_CONSTRAINT:
        return ConflictError("approval_already_pending")
    if constraint == APPROVAL_CONVERSATION_CONSTRAINT:
        return ConflictError("approval_conversation_mismatch")
    return None


def _approval_scope(scope: AccessScope) -> list[ColumnElement[bool]]:
    filters = [ApprovalRequestRow.sandbox_id == scope.sandbox_id]
    if isinstance(scope, CustomerScope):
        filters.append(ApprovalRequestRow.customer_id == scope.customer_id)
    return filters


def _proposal_json(proposal: ProposedAction) -> dict[str, Any]:
    return {
        "kind": proposal.kind.value,
        "reason": proposal.reason.value,
        "amount_cents": proposal.amount.cents,
        "items": [
            {"order_item_id": str(item.order_item_id), "quantity": item.quantity}
            for item in proposal.items
        ],
    }


def _proposal_from_json(raw: dict[str, Any]) -> ProposedAction:
    return ProposedAction(
        kind=ActionKind(raw["kind"]),
        reason=ClaimReason(raw["reason"]),
        amount=Money(raw["amount_cents"]),
        items=tuple(
            ClaimedItem(OrderItemId(UUID(item["order_item_id"])), item["quantity"])
            for item in raw["items"]
        ),
    )


def _approval_values(request: ApprovalRequest) -> dict[str, Any]:
    fraud = request.fraud
    return {
        "id": request.id,
        "sandbox_id": request.sandbox_id,
        "customer_id": request.customer_id,
        "conversation_id": request.conversation_id,
        "order_id": request.order_id,
        "proposal_key": request.proposal_key,
        "proposal": _proposal_json(request.proposal),
        "claimed_at": request.claimed_at,
        "policy_reasons": [reason.value for reason in request.policy.reasons],
        "policy_ceiling_cents": request.policy.ceiling.cents,
        "policy_document": request.policy.document.value,
        "fraud_score": None if fraud is None else fraud.score,
        "fraud_model_version": None if fraud is None else fraud.model_version,
        "fraud_factors": (
            None
            if fraud is None
            else [
                {"feature": factor.feature, "contribution": factor.contribution}
                for factor in fraud.factors
            ]
        ),
        "status": request.status.value,
        "version": request.version,
        "created_at": request.created_at,
    }


def _to_approval(row: ApprovalRequestRow) -> ApprovalRequest:
    return ApprovalRequest(
        id=ApprovalRequestId(row.id),
        sandbox_id=SandboxId(row.sandbox_id),
        customer_id=CustomerId(row.customer_id),
        conversation_id=ConversationId(row.conversation_id),
        order_id=OrderId(row.order_id),
        proposal_key=row.proposal_key,
        proposal=_proposal_from_json(row.proposal),
        claimed_at=row.claimed_at,
        policy=NeedsApproval(
            ceiling=Money(row.policy_ceiling_cents),
            document=PolicyDocumentId(row.policy_document),
            reasons=tuple(ApprovalReason(reason) for reason in row.policy_reasons),
        ),
        fraud=_to_fraud(row),
        status=ApprovalStatus(row.status),
        version=row.version,
        decided_by=None if row.decided_by is None else ReviewerId(row.decided_by),
        decided_at=row.decided_at,
        decision_reason=row.decision_reason,
        created_at=row.created_at,
    )


def _to_fraud(row: ApprovalRequestRow) -> FraudAssessment | None:
    if row.fraud_score is None or row.fraud_model_version is None or row.fraud_factors is None:
        return None
    return FraudAssessment(
        score=row.fraud_score,
        model_version=row.fraud_model_version,
        factors=tuple(
            FraudFactor(factor["feature"], factor["contribution"]) for factor in row.fraud_factors
        ),
    )


def _handoff_values(handoff: Handoff) -> dict[str, Any]:
    return {
        "id": handoff.id,
        "sandbox_id": handoff.sandbox_id,
        "customer_id": handoff.customer_id,
        "conversation_id": handoff.conversation_id,
        "priority": handoff.priority.value,
        "summary": {
            "intent": handoff.summary.intent,
            "facts": list(handoff.summary.facts),
            "actions_taken": list(handoff.summary.actions_taken),
        },
        "status": handoff.status.value,
        "assigned_to": handoff.assigned_to,
        "assigned_at": handoff.assigned_at,
        "created_at": handoff.created_at,
    }


def _to_handoff(row: HandoffRow) -> Handoff:
    return Handoff(
        id=HandoffId(row.id),
        sandbox_id=SandboxId(row.sandbox_id),
        customer_id=CustomerId(row.customer_id),
        conversation_id=ConversationId(row.conversation_id),
        priority=Priority(row.priority),
        summary=HandoffSummary(
            intent=row.summary["intent"],
            facts=tuple(row.summary["facts"]),
            actions_taken=tuple(row.summary["actions_taken"]),
        ),
        status=HandoffStatus(row.status),
        assigned_to=None if row.assigned_to is None else ReviewerId(row.assigned_to),
        assigned_at=row.assigned_at,
        created_at=row.created_at,
    )
