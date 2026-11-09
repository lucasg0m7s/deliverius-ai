"""The `OrderLocker` contract, run against every implementation (Liskov substitution, tested).

A subclass provides the `store` fixture. Waiting for a lock is not part of this contract: only
Postgres can wait, and its integration tests prove it.
"""

from collections.abc import Sequence
from datetime import timedelta
from typing import Protocol

import pytest
from support.order_builders import BASE_TIME, build_item, build_order, build_order_in

from deliverius.orders.models import EventActor, Order, OrderStatus
from deliverius.orders.ports import OrderLocker
from deliverius.shared.access import CustomerScope
from deliverius.shared.errors import InvariantError, NotFoundError
from deliverius.shared.ids import CustomerId, OrderId, SandboxId, new_id

CANCELLED_AT = BASE_TIME + timedelta(hours=3)


class LockerStore(Protocol):
    locker: OrderLocker

    async def add(self, orders: Sequence[Order]) -> None: ...


def scope_of(order: Order) -> CustomerScope:
    return CustomerScope(order.sandbox_id, order.customer_id)


def order_in(status: OrderStatus) -> Order:
    return build_order_in(status, (build_item("Pizza", 4_000),))


class OrderLockerContract:
    @pytest.fixture
    def store(self) -> LockerStore:
        raise NotImplementedError

    async def test_owner_locks_their_customer(self, store: LockerStore) -> None:
        order = build_order(sandbox_id=SandboxId(new_id()), customer_id=CustomerId(new_id()))
        await store.add([order])

        assert await store.locker.lock_customer(scope_of(order)) == order.customer_id

    async def test_customer_of_another_sandbox_or_unknown_is_not_locked(
        self, store: LockerStore
    ) -> None:
        order = build_order(sandbox_id=SandboxId(new_id()), customer_id=CustomerId(new_id()))
        await store.add([order])

        wrong_sandbox = CustomerScope(SandboxId(new_id()), order.customer_id)
        unknown = CustomerScope(order.sandbox_id, CustomerId(new_id()))

        assert await store.locker.lock_customer(wrong_sandbox) is None
        assert await store.locker.lock_customer(unknown) is None

    async def test_owner_locks_the_order_and_reads_it_with_items_and_timeline(
        self, store: LockerStore
    ) -> None:
        order = build_order(
            sandbox_id=SandboxId(new_id()), customer_id=CustomerId(new_id()), item_count=3
        )
        await store.add([order])

        assert await store.locker.lock_order(scope_of(order), order.id) == order

    async def test_order_of_another_customer_is_not_locked(self, store: LockerStore) -> None:
        order = build_order(sandbox_id=SandboxId(new_id()), customer_id=CustomerId(new_id()))
        await store.add([order])
        neighbour = CustomerScope(order.sandbox_id, CustomerId(new_id()))

        assert await store.locker.lock_order(neighbour, order.id) is None

    async def test_order_of_another_sandbox_is_not_locked(self, store: LockerStore) -> None:
        order = build_order(sandbox_id=SandboxId(new_id()), customer_id=CustomerId(new_id()))
        await store.add([order])
        elsewhere = CustomerScope(SandboxId(new_id()), order.customer_id)

        assert await store.locker.lock_order(elsewhere, order.id) is None

    async def test_unknown_order_is_not_locked(self, store: LockerStore) -> None:
        scope = CustomerScope(SandboxId(new_id()), CustomerId(new_id()))

        assert await store.locker.lock_order(scope, OrderId(new_id())) is None

    @pytest.mark.parametrize(
        "status",
        [
            OrderStatus.PLACED,
            OrderStatus.ACCEPTED,
            OrderStatus.PREPARING,
            OrderStatus.READY_FOR_PICKUP,
        ],
    )
    async def test_cancelling_before_dispatch_changes_the_status_and_adds_the_event(
        self, store: LockerStore, status: OrderStatus
    ) -> None:
        order = order_in(status)
        await store.add([order])

        await store.locker.mark_cancelled(scope_of(order), order.id, CANCELLED_AT)

        cancelled = await store.locker.lock_order(scope_of(order), order.id)
        assert cancelled is not None
        assert cancelled.status is OrderStatus.CANCELLED
        assert len(cancelled.events) == len(order.events) + 1
        last = cancelled.events[-1]
        assert (last.status, last.actor, last.occurred_at) == (
            OrderStatus.CANCELLED,
            EventActor.SYSTEM,
            CANCELLED_AT,
        )

    @pytest.mark.parametrize(
        "status",
        [
            OrderStatus.OUT_FOR_DELIVERY,
            OrderStatus.DELIVERED,
            OrderStatus.CANCELLED,
            OrderStatus.DELIVERY_FAILED,
        ],
    )
    async def test_cancelling_after_dispatch_or_twice_is_an_invariant_error_and_changes_nothing(
        self, store: LockerStore, status: OrderStatus
    ) -> None:
        order = order_in(status)
        await store.add([order])

        with pytest.raises(InvariantError) as error:
            await store.locker.mark_cancelled(scope_of(order), order.id, CANCELLED_AT)

        assert error.value.rule == "order.cancel_transition_not_allowed"
        assert await store.locker.lock_order(scope_of(order), order.id) == order

    async def test_cancelling_an_order_of_another_customer_is_not_found_and_changes_nothing(
        self, store: LockerStore
    ) -> None:
        order = order_in(OrderStatus.PLACED)
        await store.add([order])
        neighbour = CustomerScope(order.sandbox_id, CustomerId(new_id()))

        with pytest.raises(NotFoundError):
            await store.locker.mark_cancelled(neighbour, order.id, CANCELLED_AT)

        assert await store.locker.lock_order(scope_of(order), order.id) == order

    async def test_cancelling_an_unknown_order_is_not_found(self, store: LockerStore) -> None:
        scope = CustomerScope(SandboxId(new_id()), CustomerId(new_id()))

        with pytest.raises(NotFoundError):
            await store.locker.mark_cancelled(scope, OrderId(new_id()), CANCELLED_AT)

    async def test_cancelling_at_a_naive_instant_is_an_invariant_error(
        self, store: LockerStore
    ) -> None:
        order = order_in(OrderStatus.PLACED)
        await store.add([order])

        with pytest.raises(InvariantError) as error:
            await store.locker.mark_cancelled(
                scope_of(order),
                order.id,
                CANCELLED_AT.replace(tzinfo=None),
            )

        assert error.value.rule == "naive_datetime.at"
