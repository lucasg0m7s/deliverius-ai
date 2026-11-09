import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence

import pytest
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from support.database import settings_for
from support.order_builders import build_order
from support.order_locker_contract import CANCELLED_AT, LockerStore, OrderLockerContract, scope_of
from support.order_rows import insert_orders

from deliverius.db.engine import create_engine
from deliverius.db.errors import DatabaseTimeoutError
from deliverius.db.session import create_session_factory
from deliverius.db.tables import CustomerRow, DeliveryEventRow, OrderItemRow, OrderRow
from deliverius.orders.models import Order, OrderStatus
from deliverius.orders.repository import PostgresOrderRepository
from deliverius.shared.ids import CustomerId, SandboxId, new_id

pytestmark = pytest.mark.integration


class PostgresStore:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.locker = PostgresOrderRepository(session)

    async def add(self, orders: Sequence[Order]) -> None:
        await insert_orders(self.session, orders)


class TestPostgresOrderLocker(OrderLockerContract):
    @pytest.fixture
    def store(self, session: AsyncSession) -> LockerStore:
        return PostgresStore(session)


@pytest.fixture
async def committed_orders(engine: AsyncEngine) -> AsyncIterator[tuple[Order, Order]]:
    """Two orders of two customers, committed so separate transactions can contend for them."""
    sandbox_id = SandboxId(new_id())
    orders = (
        build_order(sandbox_id=sandbox_id, customer_id=CustomerId(new_id()), delivered=False),
        build_order(sandbox_id=sandbox_id, customer_id=CustomerId(new_id()), delivered=False),
    )
    async with AsyncSession(engine) as session, session.begin():
        await insert_orders(session, orders)
    try:
        yield orders
    finally:
        order_ids = [order.id for order in orders]
        async with AsyncSession(engine) as session, session.begin():
            await session.execute(
                delete(DeliveryEventRow).where(DeliveryEventRow.order_id.in_(order_ids))
            )
            await session.execute(delete(OrderItemRow).where(OrderItemRow.order_id.in_(order_ids)))
            await session.execute(delete(OrderRow).where(OrderRow.id.in_(order_ids)))
            await session.execute(
                delete(CustomerRow).where(CustomerRow.id.in_([o.customer_id for o in orders]))
            )


async def wait_until_a_statement_waits_for_a_lock(engine: AsyncEngine) -> None:
    """Polls `pg_stat_activity`: the contender is provably blocked, with no guess about timing."""
    for _ in range(200):
        async with engine.connect() as connection:
            waiting = await connection.scalar(
                text("SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock'")
            )
        if waiting:
            return
        await asyncio.sleep(0.025)
    pytest.fail("no statement ever waited for a lock")


async def test_second_transaction_locking_the_same_customer_waits_for_the_first(
    engine: AsyncEngine, committed_orders: tuple[Order, Order]
) -> None:
    scope = scope_of(committed_orders[0])
    factory = create_session_factory(engine)

    async def second_locks() -> CustomerId | None:
        async with factory() as session, session.begin():
            return await PostgresOrderRepository(session).lock_customer(scope)

    async with factory() as first, first.begin():
        assert await PostgresOrderRepository(first).lock_customer(scope) == scope.customer_id
        second = asyncio.create_task(second_locks())
        await wait_until_a_statement_waits_for_a_lock(engine)
        assert not second.done()

    assert await second == scope.customer_id


async def test_locking_another_customer_does_not_wait(
    engine: AsyncEngine, committed_orders: tuple[Order, Order]
) -> None:
    mine, other = (scope_of(order) for order in committed_orders)
    factory = create_session_factory(engine)

    async with factory() as first, first.begin(), factory() as second, second.begin():
        await PostgresOrderRepository(first).lock_customer(mine)

        async with asyncio.timeout(1):
            assert await PostgresOrderRepository(second).lock_customer(other) == other.customer_id


async def test_second_transaction_locking_the_same_order_waits_and_then_reads_it_fresh(
    engine: AsyncEngine, committed_orders: tuple[Order, Order]
) -> None:
    order = committed_orders[0]
    scope = scope_of(order)
    factory = create_session_factory(engine)

    async def second_locks() -> Order | None:
        async with factory() as session, session.begin():
            return await PostgresOrderRepository(session).lock_order(scope, order.id)

    async with factory() as first, first.begin():
        locker = PostgresOrderRepository(first)
        assert await locker.lock_order(scope, order.id) == order
        await locker.mark_cancelled(scope, order.id, CANCELLED_AT)
        second = asyncio.create_task(second_locks())
        await wait_until_a_statement_waits_for_a_lock(engine)
        assert not second.done()

    seen = await second
    assert seen is not None
    assert seen.status is OrderStatus.CANCELLED
    assert seen.events[-1].status is OrderStatus.CANCELLED


async def hold_both_locks_then_try(
    database_url: str, order: Order, attempt: Callable[[PostgresOrderRepository], Awaitable[object]]
) -> None:
    """A holder keeps the customer and the order; a waiter on a 100 ms lock timeout tries."""
    scope = scope_of(order)
    impatient = create_engine(settings_for(database_url, database_lock_timeout_ms=100))
    try:
        factory = create_session_factory(impatient)
        async with factory() as holder, holder.begin(), factory() as waiter, waiter.begin():
            await PostgresOrderRepository(holder).lock_customer(scope)
            await PostgresOrderRepository(holder).lock_order(scope, order.id)
            await attempt(PostgresOrderRepository(waiter))
    finally:
        await impatient.dispose()


async def test_customer_lock_wait_above_lock_timeout_becomes_a_database_timeout_error(
    migrated_database_url: str, committed_orders: tuple[Order, Order]
) -> None:
    order = committed_orders[0]

    with pytest.raises(DatabaseTimeoutError):
        await hold_both_locks_then_try(
            migrated_database_url, order, lambda waiter: waiter.lock_customer(scope_of(order))
        )


async def test_order_lock_wait_above_lock_timeout_becomes_a_database_timeout_error(
    migrated_database_url: str, committed_orders: tuple[Order, Order]
) -> None:
    order = committed_orders[0]

    with pytest.raises(DatabaseTimeoutError):
        await hold_both_locks_then_try(
            migrated_database_url,
            order,
            lambda waiter: waiter.lock_order(scope_of(order), order.id),
        )
