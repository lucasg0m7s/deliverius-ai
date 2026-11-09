from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Protocol

from deliverius.actions.models import ExecutedAction
from deliverius.orders import OrderLocker
from deliverius.policy import CustomerHistory
from deliverius.shared.access import AccessScope, CustomerScope
from deliverius.shared.audit import AuditSink
from deliverius.shared.ids import OrderId


class ActionStore(Protocol):
    """Executed actions. Every method filters by the scope it receives."""

    async def history(
        self, scope: CustomerScope, order_id: OrderId, since: datetime
    ) -> CustomerHistory:
        """The customer's actions executed from `since` on, plus every action of the order."""
        ...

    async def find_by_key(
        self, scope: CustomerScope, idempotency_key: str
    ) -> ExecutedAction | None: ...

    async def insert(self, action: ExecutedAction) -> ExecutedAction:
        """Store the action and return it; if its key was already stored, return that one instead.

        Raises `ConflictError` with code `action_approval_mismatch` when the approval is not an
        approved one of this conversation and proposal, and `action_conversation_mismatch` when
        the conversation is not the customer's, for this order.
        """
        ...

    async def list_for_order(
        self, scope: AccessScope, order_id: OrderId
    ) -> tuple[ExecutedAction, ...]:
        """Oldest first."""
        ...


class ExecutionUnit(Protocol):
    """What one execution transaction touches; all of it commits or rolls back together."""

    @property
    def orders(self) -> OrderLocker: ...

    @property
    def actions(self) -> ActionStore: ...

    @property
    def audit(self) -> AuditSink: ...


type UnitOfWork = Callable[[], AbstractAsyncContextManager[ExecutionUnit]]
