from collections.abc import Sequence
from datetime import datetime
from types import TracebackType
from typing import Self

from deliverius.actions.models import ExecutedAction
from deliverius.orders import InMemoryOrderRepository, Order
from deliverius.policy import CustomerHistory
from deliverius.shared.access import AccessScope, CustomerScope, is_visible
from deliverius.shared.audit import InMemoryAuditSink
from deliverius.shared.ids import ActionId, OrderId


class InMemoryActionStore:
    """`ActionStore` kept in a dict, with the same contract as Postgres (scope, order, replay).

    The foreign keys of the database (approval, conversation) are not simulated here.
    """

    def __init__(self) -> None:
        self._actions: dict[ActionId, ExecutedAction] = {}

    async def history(
        self, scope: CustomerScope, order_id: OrderId, since: datetime
    ) -> CustomerHistory:
        relevant = [
            action
            for action in self._ordered()
            if is_visible(scope, action.sandbox_id, action.customer_id)
            and (action.executed_at >= since or action.order_id == order_id)
        ]
        return CustomerHistory(actions=tuple(action.as_prior() for action in relevant))

    async def find_by_key(
        self, scope: CustomerScope, idempotency_key: str
    ) -> ExecutedAction | None:
        return next(
            (
                action
                for action in self._actions.values()
                if action.idempotency_key == idempotency_key
                and is_visible(scope, action.sandbox_id, action.customer_id)
            ),
            None,
        )

    async def insert(self, action: ExecutedAction) -> ExecutedAction:
        scope = CustomerScope(action.sandbox_id, action.customer_id)
        existing = await self.find_by_key(scope, action.idempotency_key)
        if existing is not None:
            return existing
        self._actions[action.id] = action
        return action

    async def list_for_order(
        self, scope: AccessScope, order_id: OrderId
    ) -> tuple[ExecutedAction, ...]:
        return tuple(
            action
            for action in self._ordered()
            if action.order_id == order_id
            and is_visible(scope, action.sandbox_id, action.customer_id)
        )

    def _ordered(self) -> Sequence[ExecutedAction]:
        return sorted(self._actions.values(), key=lambda action: (action.executed_at, action.id))


class InMemoryUnitOfWork:
    """Orders, actions and audit in memory; the unit of work and its own transaction at once.

    There is no rollback: atomicity is proved against Postgres.
    """

    def __init__(self, orders: Sequence[Order] = ()) -> None:
        self.orders = InMemoryOrderRepository(list(orders))
        self.actions = InMemoryActionStore()
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
