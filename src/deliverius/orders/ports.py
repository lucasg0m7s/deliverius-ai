from datetime import datetime
from typing import Protocol

from deliverius.orders.cursor import Page, PageRequest
from deliverius.orders.models import Order, OrderSummary
from deliverius.shared.access import AccessScope, CustomerScope
from deliverius.shared.ids import CustomerId, OrderId


class OrderReader(Protocol):
    """Read access to orders. The scope is mandatory: there is no way to read without one."""

    async def get_order(self, scope: AccessScope, order_id: OrderId) -> Order | None:
        """The order with items and timeline, or `None` if missing or outside the scope."""
        ...

    async def list_orders(self, scope: AccessScope, page: PageRequest) -> Page[OrderSummary]:
        """Orders in the scope, newest first. Raises `InvalidInputError` for a bad cursor."""
        ...


class OrderLocker(Protocol):
    """Locks and changes orders inside the caller's transaction; the scope is mandatory.

    Locks are taken in one fixed order, customer then order, so concurrent callers never deadlock.
    """

    async def lock_customer(self, scope: CustomerScope) -> CustomerId | None:
        """Lock the customer row until the transaction ends. `None` if missing or out of scope."""
        ...

    async def lock_order(self, scope: CustomerScope, order_id: OrderId) -> Order | None:
        """Lock the order and read it fresh, with items and timeline. `None` if out of scope."""
        ...

    async def mark_cancelled(self, scope: CustomerScope, order_id: OrderId, at: datetime) -> None:
        """Set `cancelled` and add the `cancelled` event by `system`, if the transition is allowed.

        Raises `NotFoundError` if the order is missing or out of scope and `InvariantError` if the
        order can no longer be cancelled.
        """
        ...
