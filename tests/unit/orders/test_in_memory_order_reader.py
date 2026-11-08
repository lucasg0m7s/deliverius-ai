from collections.abc import Sequence

import pytest
from support.order_builders import build_order
from support.order_reader_contract import OrderReaderContract, OrderStore

from deliverius.orders.in_memory import InMemoryOrderRepository
from deliverius.orders.models import Order
from deliverius.shared.access import SandboxScope
from deliverius.shared.ids import CustomerId, SandboxId, new_id


class InMemoryStore:
    def __init__(self) -> None:
        self.reader = InMemoryOrderRepository()

    async def add(self, orders: Sequence[Order]) -> None:
        for order in orders:
            self.reader.add(order)


class TestInMemoryOrderReader(OrderReaderContract):
    @pytest.fixture
    def store(self) -> OrderStore:
        return InMemoryStore()


async def test_constructor_accepts_initial_orders() -> None:
    sandbox_id = SandboxId(new_id())
    order = build_order(sandbox_id=sandbox_id, customer_id=CustomerId(new_id()))

    repository = InMemoryOrderRepository([order])

    assert await repository.get_order(SandboxScope(sandbox_id), order.id) == order
