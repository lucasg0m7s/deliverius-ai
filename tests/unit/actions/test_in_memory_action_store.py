import pytest
from support.action_store_contract import ActionStoreContract, StoreHarness

from deliverius.actions import InMemoryActionStore
from deliverius.orders import Order
from deliverius.shared import ConversationId, new_id


class InMemoryHarness:
    def __init__(self) -> None:
        self.store = InMemoryActionStore()
        self._conversations: dict[Order, ConversationId] = {}

    async def add_order(self, order: Order) -> ConversationId:
        return self._conversations.setdefault(order, ConversationId(new_id()))


class TestInMemoryActionStore(ActionStoreContract):
    @pytest.fixture
    def harness(self) -> StoreHarness:
        return InMemoryHarness()
