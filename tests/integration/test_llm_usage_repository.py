"""`PostgresLlmUsageStore`: the shared contract, then the races only a real database shows."""

import asyncio
from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from support.llm_usage_contract import LlmUsageStoreContract, UsageHarness

from deliverius.db.tables import LlmBudgetUsageRow, LlmCallRow
from deliverius.llm.ports import BudgetLimit, BudgetUsage, CallStatus, LlmCallRecord
from deliverius.llm.pricing import TokenUsage
from deliverius.llm.repository import PostgresLlmUsageStore
from deliverius.llm.roles import ModelRole
from deliverius.shared.model_ref import LlmProvider

pytestmark = pytest.mark.integration

DAY = date(2026, 11, 7)
GEMINI = LlmProvider.GEMINI
PARALLEL_CALLS = 50


class PostgresHarness:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self.factory = factory
        self.store = PostgresLlmUsageStore(factory)

    async def usage(self, provider: LlmProvider, day: date) -> BudgetUsage | None:
        async with self.factory() as session:
            row = await session.get(LlmBudgetUsageRow, (provider.value, day))
        return None if row is None else BudgetUsage(row.requests_used, row.tokens_used)

    async def calls(self) -> list[LlmCallRecord]:
        async with self.factory() as session:
            rows = (await session.execute(select(LlmCallRow))).scalars().all()
        return [_record_of(row) for row in rows]


def _record_of(row: LlmCallRow) -> LlmCallRecord:
    return LlmCallRecord(
        id=row.id,
        created_at=row.created_at,
        role=ModelRole(row.role),
        provider=LlmProvider(row.provider),
        model=row.model,
        status=CallStatus(row.status),
        latency_ms=row.latency_ms,
        usage=TokenUsage(row.input_tokens, row.output_tokens, row.reasoning_tokens),
        conversation_id=row.conversation_id,
        sandbox_id=row.sandbox_id,
        prompt_name=row.prompt_name,
        prompt_version=row.prompt_version,
        prompt_hash=row.prompt_hash,
        error_type=row.error_type,
        real_cost_usd_micros=row.real_cost_usd_micros,
        equivalent_cost_usd_micros=row.equivalent_cost_usd_micros,
        trace_id=row.trace_id,
    )


class TestPostgresLlmUsageStore(LlmUsageStoreContract):
    @pytest.fixture
    def harness(self, llm_usage_factory: async_sessionmaker[AsyncSession]) -> UsageHarness:
        return PostgresHarness(llm_usage_factory)


@pytest.fixture
def harness(llm_usage_factory: async_sessionmaker[AsyncSession]) -> PostgresHarness:
    return PostgresHarness(llm_usage_factory)


async def test_fifty_parallel_reservations_with_ten_left_grant_exactly_ten(
    harness: PostgresHarness,
) -> None:
    limit = BudgetLimit(daily_requests=15, daily_tokens=None)
    for _ in range(5):
        assert await harness.store.reserve(GEMINI, DAY, limit) is not None

    results = await asyncio.gather(
        *(harness.store.reserve(GEMINI, DAY, limit) for _ in range(PARALLEL_CALLS))
    )

    granted = [count for count in results if count is not None]
    assert len(granted) == 10
    assert results.count(None) == 40
    assert sorted(granted) == list(range(6, 16))
    assert await harness.usage(GEMINI, DAY) == BudgetUsage(15, 0)


async def test_parallel_reservations_racing_to_create_the_first_row_still_stop_at_the_limit(
    harness: PostgresHarness,
) -> None:
    limit = BudgetLimit(daily_requests=10, daily_tokens=None)

    results = await asyncio.gather(
        *(harness.store.reserve(GEMINI, DAY, limit) for _ in range(PARALLEL_CALLS))
    )

    assert sorted(count for count in results if count is not None) == list(range(1, 11))
    assert await harness.usage(GEMINI, DAY) == BudgetUsage(10, 0)


async def test_parallel_token_sums_lose_nothing(harness: PostgresHarness) -> None:
    amounts = list(range(1, PARALLEL_CALLS + 1))

    totals = await asyncio.gather(*(harness.store.add_tokens(GEMINI, DAY, n) for n in amounts))

    assert await harness.usage(GEMINI, DAY) == BudgetUsage(0, sum(amounts))
    assert max(totals) == sum(amounts)
    assert len(set(totals)) == PARALLEL_CALLS


async def test_parallel_token_sums_over_an_existing_row_lose_nothing(
    harness: PostgresHarness,
) -> None:
    assert await harness.store.reserve(GEMINI, DAY, BudgetLimit(100, None)) == 1

    await asyncio.gather(
        *(harness.store.add_tokens(GEMINI, DAY, 10) for _ in range(PARALLEL_CALLS))
    )

    assert await harness.usage(GEMINI, DAY) == BudgetUsage(1, 500)


async def test_the_token_limit_stops_the_next_reservation_after_parallel_sums(
    harness: PostgresHarness,
) -> None:
    limit = BudgetLimit(daily_requests=1_000, daily_tokens=500)
    assert await harness.store.reserve(GEMINI, DAY, limit) == 1

    await asyncio.gather(
        *(harness.store.add_tokens(GEMINI, DAY, 10) for _ in range(PARALLEL_CALLS))
    )

    assert await harness.store.reserve(GEMINI, DAY, limit) is None
    assert await harness.usage(GEMINI, DAY) == BudgetUsage(1, 500)


async def test_reservations_and_token_sums_running_together_stay_consistent(
    harness: PostgresHarness,
) -> None:
    limit = BudgetLimit(daily_requests=PARALLEL_CALLS, daily_tokens=None)

    await asyncio.gather(
        *(harness.store.reserve(GEMINI, DAY, limit) for _ in range(PARALLEL_CALLS)),
        *(harness.store.add_tokens(GEMINI, DAY, 2) for _ in range(PARALLEL_CALLS)),
    )

    assert await harness.usage(GEMINI, DAY) == BudgetUsage(PARALLEL_CALLS, 2 * PARALLEL_CALLS)
