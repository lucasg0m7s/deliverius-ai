from typing import assert_never

import structlog

from deliverius.actions.models import (
    Authority,
    Automatic,
    Executed,
    ExecutedAction,
    ExecutionRequest,
    ExecutionResult,
    NotExecuted,
    ReviewerApproved,
)
from deliverius.actions.ports import ExecutionUnit, UnitOfWork
from deliverius.db.errors import DatabaseTimeoutError
from deliverius.orders import Order, OrderLocker
from deliverius.policy import (
    POLICY_LIMITS,
    ActionKind,
    Allowed,
    ApprovedBy,
    Denied,
    NeedsApproval,
    PolicyDecision,
    evaluate,
    proposal_key,
)
from deliverius.shared.access import CustomerScope
from deliverius.shared.audit import AuditActorType, AuditEntry, JsonValue
from deliverius.shared.clock import Clock
from deliverius.shared.errors import ConflictError, NotFoundError
from deliverius.shared.ids import ActionId, ApprovalRequestId, OrderId, new_id

logger = structlog.get_logger(__name__)

_MISMATCH_EVENTS = {
    "action_approval_mismatch": "actions.approval_mismatch",
    "action_conversation_mismatch": "actions.conversation_mismatch",
}


class ActionExecutor:
    """The only writer of actions: lock, re-check the policy, store and audit in one transaction.

    Locks go customer first, then order, so executions never deadlock. The history and the order are
    read after the locks, so two executions cannot both pass a ceiling or a limit alone.
    """

    def __init__(self, unit_of_work: UnitOfWork, clock: Clock) -> None:
        self._unit_of_work = unit_of_work
        self._clock = clock

    async def execute(self, scope: CustomerScope, request: ExecutionRequest) -> ExecutionResult:
        async with self._unit_of_work() as unit:
            order = await _lock(unit.orders, scope, request.order_id)
            key = proposal_key(request.conversation_id, order.id, request.proposal)
            existing = await unit.actions.find_by_key(scope, key)
            if existing is not None:
                return _replayed(existing)
            since = request.claimed_at - POLICY_LIMITS.customer_auto_approval_window
            history = await unit.actions.history(scope, order.id, since)
            decision = evaluate(request.proposal, order, history, request.claimed_at)
            refusal = _refusal(decision, request.authority, order.id)
            if refusal is not None:
                return refusal
            action = self._build(scope, request, key)
            stored = await _insert(unit, action)
            if stored.id != action.id:
                return _replayed(stored)
            await self._apply(unit, scope, action, decision, request.authority)
            return Executed(action, replayed=False)

    def _build(self, scope: CustomerScope, request: ExecutionRequest, key: str) -> ExecutedAction:
        approved_by, approval_request_id = _approval_of(request.authority)
        return ExecutedAction(
            id=ActionId(new_id()),
            sandbox_id=scope.sandbox_id,
            customer_id=scope.customer_id,
            order_id=request.order_id,
            conversation_id=request.conversation_id,
            proposal=request.proposal,
            idempotency_key=key,
            approved_by=approved_by,
            approval_request_id=approval_request_id,
            claimed_at=request.claimed_at,
            executed_at=self._clock.now(),
        )

    async def _apply(
        self,
        unit: ExecutionUnit,
        scope: CustomerScope,
        action: ExecutedAction,
        decision: PolicyDecision,
        authority: Authority,
    ) -> None:
        if action.proposal.kind is ActionKind.CANCELLATION:
            await unit.orders.mark_cancelled(scope, action.order_id, action.executed_at)
        await unit.audit.record(_audit_entry(action, decision, authority))
        logger.info(
            "actions.executed",
            action_id=str(action.id),
            order_id=str(action.order_id),
            kind=action.proposal.kind.value,
            amount_cents=action.proposal.amount.cents,
            approved_by=action.approved_by.value,
        )


async def _lock(orders: OrderLocker, scope: CustomerScope, order_id: OrderId) -> Order:
    """Customer, then order. A lock that never comes is a conflict the caller may retry."""
    try:
        customer = await orders.lock_customer(scope)
        order = None if customer is None else await orders.lock_order(scope, order_id)
    except DatabaseTimeoutError as error:
        logger.warning("actions.lock_conflict", order_id=str(order_id))
        raise ConflictError("action_lock_timeout") from error
    if order is None:
        raise NotFoundError
    return order


def _replayed(action: ExecutedAction) -> Executed:
    logger.info("actions.replayed", action_id=str(action.id), order_id=str(action.order_id))
    return Executed(action, replayed=True)


def _refusal(
    decision: PolicyDecision, authority: Authority, order_id: OrderId
) -> NotExecuted | None:
    """A person's approval covers a case sent to a person, never one the policy denies."""
    match decision:
        case Denied(reason=reason):
            logger.info("actions.reevaluation_denied", order_id=str(order_id), reason=reason.value)
            return NotExecuted(decision)
        case NeedsApproval(reasons=reasons) if isinstance(authority, Automatic):
            logger.info(
                "actions.needs_approval",
                order_id=str(order_id),
                reasons=[reason.value for reason in reasons],
            )
            return NotExecuted(decision)
        case NeedsApproval() | Allowed():
            return None
        case _:  # pragma: no cover - mypy proves every decision is handled
            assert_never(decision)


async def _insert(unit: ExecutionUnit, action: ExecutedAction) -> ExecutedAction:
    try:
        return await unit.actions.insert(action)
    except ConflictError as error:
        logger.warning(
            _MISMATCH_EVENTS[error.code],
            action_id=str(action.id),
            order_id=str(action.order_id),
            conversation_id=str(action.conversation_id),
        )
        raise


def _approval_of(authority: Authority) -> tuple[ApprovedBy, ApprovalRequestId | None]:
    match authority:
        case Automatic():
            return ApprovedBy.AUTOMATIC, None
        case ReviewerApproved(approval_request_id=approval_request_id):
            return ApprovedBy.REVIEWER, approval_request_id
        case _:  # pragma: no cover - mypy proves every authority is handled
            assert_never(authority)


def _audit_entry(
    action: ExecutedAction, decision: PolicyDecision, authority: Authority
) -> AuditEntry:
    data: dict[str, JsonValue] = {
        "kind": action.proposal.kind.value,
        "reason": action.proposal.reason.value,
        "amount_cents": action.proposal.amount.cents,
        "approved_by": action.approved_by.value,
    }
    if isinstance(authority, ReviewerApproved):
        data["approval_request_id"] = str(authority.approval_request_id)
        data["reviewer_id"] = str(authority.reviewer_id)
    if isinstance(decision, NeedsApproval):
        data["reevaluation_reasons"] = [reason.value for reason in decision.reasons]
    return AuditEntry(
        sandbox_id=action.sandbox_id,
        actor_type=AuditActorType.AGENT,
        actor_id=None,
        action="actions.executed",
        target_type="action",
        target_id=action.id,
        occurred_at=action.executed_at,
        data=data,
    )
