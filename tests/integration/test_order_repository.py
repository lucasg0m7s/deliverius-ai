from collections.abc import Sequence
from datetime import timedelta
from typing import Any, Self

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession
from support.order_builders import BASE_TIME, build_order
from support.order_reader_contract import OrderReaderContract, OrderStore
from support.order_rows import insert_orders

from deliverius.orders.cursor import PageRequest
from deliverius.orders.models import Order
from deliverius.orders.repository import PostgresOrderRepository
from deliverius.shared.access import AccessScope, CustomerScope, SandboxScope
from deliverius.shared.ids import CustomerId, SandboxId, new_id

pytestmark = pytest.mark.integration


class PostgresStore:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.reader = PostgresOrderRepository(session)

    async def add(self, orders: Sequence[Order]) -> None:
        await insert_orders(self.session, orders)


class TestPostgresOrderReader(OrderReaderContract):
    @pytest.fixture
    def store(self, session: AsyncSession) -> OrderStore:
        return PostgresStore(session)


class StatementRecorder:
    """Records every SQL statement the session sends, with its driver parameters."""

    def __init__(self, session: AsyncSession) -> None:
        self.statements: list[tuple[str, Any]] = []
        self._engine = session.bind.engine.sync_engine  # type: ignore[union-attr]

    def __enter__(self) -> Self:
        event.listen(self._engine, "before_cursor_execute", self._record)
        return self

    def __exit__(self, *_: object) -> None:
        event.remove(self._engine, "before_cursor_execute", self._record)

    def _record(self, _conn: Any, _cursor: Any, statement: str, parameters: Any, *_: Any) -> None:
        self.statements.append((statement, parameters))


@pytest.mark.parametrize("item_count", [1, 20])
async def test_get_order_uses_three_queries_whatever_the_item_count(
    session: AsyncSession, item_count: int
) -> None:
    sandbox_id, customer_id = SandboxId(new_id()), CustomerId(new_id())
    order = build_order(sandbox_id=sandbox_id, customer_id=customer_id, item_count=item_count)
    await insert_orders(session, [order])
    repository = PostgresOrderRepository(session)

    with StatementRecorder(session) as recorder:
        found = await repository.get_order(CustomerScope(sandbox_id, customer_id), order.id)

    assert found is not None
    assert len(found.items) == item_count
    assert len(recorder.statements) == 3


async def _plan_of_listing(session: AsyncSession, scope: AccessScope, cursor: str | None) -> str:
    repository = PostgresOrderRepository(session)
    with StatementRecorder(session) as recorder:
        await repository.list_orders(scope, PageRequest(limit=10, cursor=cursor))
    [(statement, parameters)] = recorder.statements
    # Small test tables make a seq or bitmap scan plus sort look cheapest; turning them off asks
    # whether a plan that walks the index in ORDER BY order exists at all.
    await session.execute(text("SET LOCAL enable_seqscan = off"))
    await session.execute(text("SET LOCAL enable_bitmapscan = off"))
    connection = await session.connection()
    result = await connection.exec_driver_sql(f"EXPLAIN {statement}", parameters)
    return "\n".join(row[0] for row in result)


@pytest.mark.parametrize(
    ("scope_kind", "index_name"),
    [("customer", "ix_orders_customer_listing"), ("sandbox", "ix_orders_sandbox_listing")],
)
async def test_listings_use_the_composite_index(
    session: AsyncSession, scope_kind: str, index_name: str
) -> None:
    sandbox_id = SandboxId(new_id())
    customers = [CustomerId(new_id()) for _ in range(20)]
    customer_id = customers[0]
    # Many customers per sandbox, as in real data, so each index is the selective one for its scope.
    orders = [
        build_order(
            sandbox_id=sandbox_id,
            customer_id=customers[index % len(customers)],
            placed_at=BASE_TIME + timedelta(minutes=index),
            item_count=1,
        )
        for index in range(300)
    ]
    await insert_orders(session, orders)
    await session.execute(text("ANALYZE orders"))
    scope: AccessScope = (
        CustomerScope(sandbox_id, customer_id)
        if scope_kind == "customer"
        else SandboxScope(sandbox_id)
    )
    first_page = await PostgresOrderRepository(session).list_orders(scope, PageRequest(limit=10))

    first_plan = await _plan_of_listing(session, scope, None)
    next_plan = await _plan_of_listing(session, scope, first_page.next_cursor)

    assert index_name in first_plan, first_plan
    assert index_name in next_plan, next_plan
    assert "Sort" not in first_plan, first_plan
