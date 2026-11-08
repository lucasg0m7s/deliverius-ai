from typing import Protocol

from deliverius.orders.cursor import Page, PageRequest
from deliverius.orders.models import Order, OrderSummary
from deliverius.shared.access import AccessScope
from deliverius.shared.ids import OrderId


class OrderReader(Protocol):
    """Read access to orders. The scope is mandatory: there is no way to read without one."""

    async def get_order(self, scope: AccessScope, order_id: OrderId) -> Order | None:
        """The order with items and timeline, or `None` if missing or outside the scope."""
        ...

    async def list_orders(self, scope: AccessScope, page: PageRequest) -> Page[OrderSummary]:
        """Orders in the scope, newest first. Raises `InvalidInputError` for a bad cursor."""
        ...
