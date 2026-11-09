from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, assert_never
from uuid import UUID

from sqlalchemy import ColumnElement, Row, Select, literal_column, select, union
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deliverius.actions.models import ExecutedAction
from deliverius.actions.ports import ExecutionUnit, UnitOfWork
from deliverius.db.audit import PostgresAuditSink
from deliverius.db.errors import translate_db_errors, violated_constraint
from deliverius.db.tables import ActionRow
from deliverius.orders import OrderLocker, PostgresOrderRepository
from deliverius.policy import (
    ActionKind,
    ApprovedBy,
    ClaimedItem,
    ClaimReason,
    CustomerHistory,
    PriorAction,
    ProposedAction,
)
from deliverius.shared.access import AccessScope, CustomerScope, SandboxScope
from deliverius.shared.audit import AuditSink
from deliverius.shared.errors import ConflictError
from deliverius.shared.ids import (
    ActionId,
    ApprovalRequestId,
    ConversationId,
    CustomerId,
    OrderId,
    OrderItemId,
    SandboxId,
)
from deliverius.shared.money import Money

IDEMPOTENCY_KEY_CONSTRAINT = "uq_actions_idempotency_key"
APPROVAL_CONSTRAINT = "fk_actions_approval_matches_request"
CONVERSATION_CONSTRAINT = "fk_actions_conversation_matches_customer_order"

_APPROVED = "approved"


class PostgresActionStore:
    """`ActionStore` on Postgres. Uses the caller's session and never commits."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def history(
        self, scope: CustomerScope, order_id: OrderId, since: datetime
    ) -> CustomerHistory:
        columns = (
            ActionRow.id,
            ActionRow.order_id,
            ActionRow.kind,
            ActionRow.reason,
            ActionRow.amount_cents,
            ActionRow.items,
            ActionRow.executed_at,
            ActionRow.approved_by,
        )
        # Two queries joined by UNION, not one with OR: each one has its own index to walk.
        recent = select(*columns).where(*_scope_filter(scope), ActionRow.executed_at >= since)
        of_order = select(*columns).where(*_scope_filter(scope), ActionRow.order_id == order_id)
        statement = union(recent, of_order).order_by(
            literal_column("executed_at"), literal_column("id")
        )
        with translate_db_errors():
            rows = (await self._session.execute(statement)).all()
        return CustomerHistory(actions=tuple(_to_prior(row) for row in rows))

    async def find_by_key(
        self, scope: CustomerScope, idempotency_key: str
    ) -> ExecutedAction | None:
        with translate_db_errors():
            row = (
                await self._session.execute(_by_key(scope, idempotency_key))
            ).scalar_one_or_none()
        return None if row is None else _to_action(row)

    async def insert(self, action: ExecutedAction) -> ExecutedAction:
        with translate_db_errors():
            try:
                # A savepoint: a refused row must not abort the caller's transaction.
                async with self._session.begin_nested():
                    self._session.add(_to_row(action))
                    await self._session.flush()
            except IntegrityError as error:
                return await self._resolve_violation(action, error)
        return action

    async def list_for_order(
        self, scope: AccessScope, order_id: OrderId
    ) -> tuple[ExecutedAction, ...]:
        statement = (
            select(ActionRow)
            .where(ActionRow.order_id == order_id, *_scope_filter(scope))
            .order_by(ActionRow.executed_at, ActionRow.id)
        )
        with translate_db_errors():
            rows = (await self._session.execute(statement)).scalars().all()
        return tuple(_to_action(row) for row in rows)

    async def _resolve_violation(
        self, action: ExecutedAction, error: IntegrityError
    ) -> ExecutedAction:
        constraint = violated_constraint(error)
        if constraint == IDEMPOTENCY_KEY_CONSTRAINT:
            # The executor's locks already serialize this; only a caller that skips them gets here.
            scope = CustomerScope(action.sandbox_id, action.customer_id)
            stored = await self._session.execute(_by_key(scope, action.idempotency_key))
            return _to_action(stored.scalar_one())
        if constraint == APPROVAL_CONSTRAINT:
            raise ConflictError("action_approval_mismatch") from error
        if constraint == CONVERSATION_CONSTRAINT:
            raise ConflictError("action_conversation_mismatch") from error
        raise error


class PostgresExecutionUnit:
    """The repositories of one transaction, on one session."""

    def __init__(self, session: AsyncSession) -> None:
        self._orders = PostgresOrderRepository(session)
        self._actions = PostgresActionStore(session)
        self._audit = PostgresAuditSink(session)

    @property
    def orders(self) -> OrderLocker:
        return self._orders

    @property
    def actions(self) -> PostgresActionStore:
        return self._actions

    @property
    def audit(self) -> AuditSink:
        return self._audit


def postgres_unit_of_work(factory: async_sessionmaker[AsyncSession]) -> UnitOfWork:
    """One transaction per execution: opened here, committed on exit, rolled back on any error."""

    @asynccontextmanager
    async def open_unit() -> AsyncIterator[ExecutionUnit]:
        with translate_db_errors():
            async with factory() as session, session.begin():
                yield PostgresExecutionUnit(session)

    return open_unit


def _scope_filter(scope: AccessScope) -> list[ColumnElement[bool]]:
    match scope:
        case CustomerScope(sandbox_id=sandbox_id, customer_id=customer_id):
            return [ActionRow.sandbox_id == sandbox_id, ActionRow.customer_id == customer_id]
        case SandboxScope(sandbox_id=sandbox_id):
            return [ActionRow.sandbox_id == sandbox_id]
        case _:  # pragma: no cover - mypy proves every scope is handled
            assert_never(scope)


def _by_key(scope: CustomerScope, idempotency_key: str) -> Select[ActionRow]:
    return select(ActionRow).where(
        ActionRow.idempotency_key == idempotency_key, *_scope_filter(scope)
    )


def _items_json(items: tuple[ClaimedItem, ...]) -> list[dict[str, Any]]:
    return [{"order_item_id": str(item.order_item_id), "quantity": item.quantity} for item in items]


def _items_from_json(raw: list[dict[str, Any]]) -> tuple[ClaimedItem, ...]:
    return tuple(
        ClaimedItem(OrderItemId(UUID(item["order_item_id"])), item["quantity"]) for item in raw
    )


def _to_row(action: ExecutedAction) -> ActionRow:
    proposal = action.proposal
    return ActionRow(
        id=action.id,
        sandbox_id=action.sandbox_id,
        customer_id=action.customer_id,
        order_id=action.order_id,
        conversation_id=action.conversation_id,
        kind=proposal.kind.value,
        reason=proposal.reason.value,
        amount_cents=proposal.amount.cents,
        items=_items_json(proposal.items),
        idempotency_key=action.idempotency_key,
        approved_by=action.approved_by.value,
        approval_request_id=action.approval_request_id,
        approval_status=None if action.approval_request_id is None else _APPROVED,
        claimed_at=action.claimed_at,
        executed_at=action.executed_at,
    )


def _to_action(row: ActionRow) -> ExecutedAction:
    return ExecutedAction(
        id=ActionId(row.id),
        sandbox_id=SandboxId(row.sandbox_id),
        customer_id=CustomerId(row.customer_id),
        order_id=OrderId(row.order_id),
        conversation_id=ConversationId(row.conversation_id),
        proposal=ProposedAction(
            kind=ActionKind(row.kind),
            reason=ClaimReason(row.reason),
            amount=Money(row.amount_cents),
            items=_items_from_json(row.items),
        ),
        idempotency_key=row.idempotency_key,
        approved_by=ApprovedBy(row.approved_by),
        approval_request_id=(
            None if row.approval_request_id is None else ApprovalRequestId(row.approval_request_id)
        ),
        claimed_at=row.claimed_at,
        executed_at=row.executed_at,
    )


def _to_prior(row: Row[Any]) -> PriorAction:
    return PriorAction(
        order_id=OrderId(row.order_id),
        kind=ActionKind(row.kind),
        reason=ClaimReason(row.reason),
        amount=Money(row.amount_cents),
        items=_items_from_json(row.items),
        executed_at=row.executed_at,
        approved_by=ApprovedBy(row.approved_by),
    )
