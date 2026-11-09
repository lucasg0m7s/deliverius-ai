from collections.abc import Sequence

import pytest
from support.order_locker_contract import LockerStore, OrderLockerContract

from deliverius.orders.in_memory import InMemoryOrderRepository
from deliverius.orders.models import Order


class InMemoryStore:
    def __init__(self) -> None:
        self.locker = InMemoryOrderRepository()

    async def add(self, orders: Sequence[Order]) -> None:
        for order in orders:
            self.locker.add(order)


class TestInMemoryOrderLocker(OrderLockerContract):
    @pytest.fixture
    def store(self) -> LockerStore:
        return InMemoryStore()
