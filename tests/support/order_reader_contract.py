"""The `OrderReader` contract, run against every implementation (Liskov substitution, tested).

A subclass provides the `store` fixture; the tests here never know which implementation they hit.
"""

from collections.abc import Sequence
from datetime import timedelta
from random import Random
from typing import Protocol

import pytest
from support.order_builders import BASE_TIME, build_order

from deliverius.orders.cursor import PageRequest
from deliverius.orders.models import Order, OrderStatus
from deliverius.orders.ports import OrderReader
from deliverius.shared.access import CustomerScope, SandboxScope
from deliverius.shared.errors import InvalidInputError
from deliverius.shared.ids import CustomerId, OrderId, SandboxId, new_id


class OrderStore(Protocol):
    reader: OrderReader

    async def add(self, orders: Sequence[Order]) -> None: ...


def new_owner() -> tuple[SandboxId, CustomerId]:
    return SandboxId(new_id()), CustomerId(new_id())


class OrderReaderContract:
    @pytest.fixture
    def store(self) -> OrderStore:
        raise NotImplementedError

    async def test_owner_reads_order_with_items_and_timeline_in_order(
        self, store: OrderStore
    ) -> None:
        sandbox_id, customer_id = new_owner()
        order = build_order(sandbox_id=sandbox_id, customer_id=customer_id, item_count=4)
        await store.add([order])

        found = await store.reader.get_order(CustomerScope(sandbox_id, customer_id), order.id)

        assert found == order
        assert found is not None
        assert found.events[0].status == OrderStatus.PLACED
        assert found.events[-1].status == OrderStatus.DELIVERED
        instants = [event.occurred_at for event in found.events]
        assert instants == sorted(instants)

    async def test_other_customer_in_same_sandbox_gets_nothing(self, store: OrderStore) -> None:
        sandbox_id, customer_id = new_owner()
        order = build_order(sandbox_id=sandbox_id, customer_id=customer_id)
        await store.add([order])
        intruder = CustomerScope(sandbox_id, CustomerId(new_id()))

        assert await store.reader.get_order(intruder, order.id) is None

    async def test_other_sandbox_gets_nothing(self, store: OrderStore) -> None:
        sandbox_id, customer_id = new_owner()
        order = build_order(sandbox_id=sandbox_id, customer_id=customer_id)
        await store.add([order])
        other_sandbox = SandboxId(new_id())

        assert await store.reader.get_order(SandboxScope(other_sandbox), order.id) is None
        same_customer_wrong_sandbox = CustomerScope(other_sandbox, customer_id)
        assert await store.reader.get_order(same_customer_wrong_sandbox, order.id) is None

    async def test_unknown_id_looks_the_same_as_out_of_scope(self, store: OrderStore) -> None:
        sandbox_id, customer_id = new_owner()

        missing = await store.reader.get_order(
            CustomerScope(sandbox_id, customer_id), OrderId(new_id())
        )

        assert missing is None

    async def test_sandbox_scope_reads_any_customer_of_its_sandbox(self, store: OrderStore) -> None:
        sandbox_id, customer_id = new_owner()
        order = build_order(sandbox_id=sandbox_id, customer_id=customer_id)
        await store.add([order])

        assert await store.reader.get_order(SandboxScope(sandbox_id), order.id) == order

    async def test_listing_pages_sixty_orders_as_fifty_plus_ten_with_a_new_order_between(
        self, store: OrderStore
    ) -> None:
        sandbox_id, customer_id = new_owner()
        orders = [
            build_order(
                sandbox_id=sandbox_id,
                customer_id=customer_id,
                placed_at=BASE_TIME + timedelta(hours=index),
                item_count=1,
            )
            for index in range(60)
        ]
        Random(7).shuffle(orders)  # noqa: S311 - insertion order for a test, not security
        await store.add(orders)
        scope = CustomerScope(sandbox_id, customer_id)

        first = await store.reader.list_orders(scope, PageRequest(limit=50))
        newer = build_order(
            sandbox_id=sandbox_id,
            customer_id=customer_id,
            placed_at=BASE_TIME + timedelta(days=30),
        )
        await store.add([newer])
        second = await store.reader.list_orders(
            scope, PageRequest(limit=50, cursor=first.next_cursor)
        )

        assert len(first.items) == 50
        assert first.next_cursor is not None
        assert len(second.items) == 10
        assert second.next_cursor is None
        seen = [summary.id for summary in first.items + second.items]
        expected = [o.id for o in sorted(orders, key=lambda o: o.placed_at, reverse=True)]
        assert seen == expected

    async def test_orders_with_the_same_instant_are_neither_repeated_nor_skipped(
        self, store: OrderStore
    ) -> None:
        sandbox_id, customer_id = new_owner()
        orders = [build_order(sandbox_id=sandbox_id, customer_id=customer_id) for _ in range(5)]
        await store.add(orders)
        scope = CustomerScope(sandbox_id, customer_id)

        seen: list[OrderId] = []
        cursor: str | None = None
        for _ in range(5):
            page = await store.reader.list_orders(scope, PageRequest(limit=2, cursor=cursor))
            seen.extend(summary.id for summary in page.items)
            cursor = page.next_cursor
            if cursor is None:
                break

        assert sorted(seen, reverse=True) == seen
        assert set(seen) == {order.id for order in orders}
        assert len(seen) == 5

    async def test_listing_respects_the_scope(self, store: OrderStore) -> None:
        sandbox_id, customer_id = new_owner()
        mine = build_order(sandbox_id=sandbox_id, customer_id=customer_id)
        neighbour = build_order(sandbox_id=sandbox_id, customer_id=CustomerId(new_id()))
        elsewhere = build_order(sandbox_id=SandboxId(new_id()), customer_id=CustomerId(new_id()))
        await store.add([mine, neighbour, elsewhere])

        own = await store.reader.list_orders(CustomerScope(sandbox_id, customer_id), PageRequest())
        whole = await store.reader.list_orders(SandboxScope(sandbox_id), PageRequest())

        assert [summary.id for summary in own.items] == [mine.id]
        assert {summary.id for summary in whole.items} == {mine.id, neighbour.id}

    async def test_page_size_above_the_maximum_returns_at_most_fifty(
        self, store: OrderStore
    ) -> None:
        sandbox_id, customer_id = new_owner()
        await store.add(
            [
                build_order(
                    sandbox_id=sandbox_id,
                    customer_id=customer_id,
                    placed_at=BASE_TIME + timedelta(minutes=index),
                    item_count=1,
                )
                for index in range(55)
            ]
        )

        page = await store.reader.list_orders(
            CustomerScope(sandbox_id, customer_id), PageRequest(limit=500)
        )

        assert len(page.items) == 50

    async def test_tampered_cursor_is_invalid_input(self, store: OrderStore) -> None:
        sandbox_id, customer_id = new_owner()

        with pytest.raises(InvalidInputError):
            await store.reader.list_orders(
                CustomerScope(sandbox_id, customer_id), PageRequest(cursor="eyJ0YW1wZXJlZCI6MX0")
            )
