"""LLM usage tables: the database refuses impossible values and the indexes serve the queries."""

from datetime import date
from typing import Any

import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from support.llm_rows import call_values

from deliverius.db.tables import LlmBudgetUsageRow, LlmCallRow

pytestmark = pytest.mark.integration


async def assert_refused(session: AsyncSession, table: Any, values: dict[str, Any]) -> None:
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await session.execute(insert(table).values(**values))


async def test_a_complete_call_row_is_accepted(session: AsyncSession) -> None:
    values = call_values()

    await session.execute(insert(LlmCallRow).values(**values))

    row = (await session.execute(select(LlmCallRow).where(LlmCallRow.id == values["id"]))).scalar()
    assert row is not None
    assert (row.input_tokens, row.equivalent_cost_usd_micros) == (100, 80)


async def test_unknown_values_stay_empty_instead_of_zero(session: AsyncSession) -> None:
    values = call_values(
        input_tokens=None,
        output_tokens=None,
        reasoning_tokens=None,
        real_cost_usd_micros=None,
        equivalent_cost_usd_micros=None,
    )

    await session.execute(insert(LlmCallRow).values(**values))

    row = (await session.execute(select(LlmCallRow).where(LlmCallRow.id == values["id"]))).scalar()
    assert row is not None
    assert row.input_tokens is None
    assert row.equivalent_cost_usd_micros is None
    assert row.conversation_id is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"input_tokens": -1},
        {"output_tokens": -1},
        {"reasoning_tokens": -1},
        {"real_cost_usd_micros": -1},
        {"equivalent_cost_usd_micros": -1},
        {"latency_ms": -1},
        {"status": "unknown"},
        {"status": "OK"},
    ],
    ids=[
        "negative-input-tokens",
        "negative-output-tokens",
        "negative-reasoning-tokens",
        "negative-real-cost",
        "negative-equivalent-cost",
        "negative-latency",
        "unknown-status",
        "status-case",
    ],
)
async def test_impossible_call_row_is_refused(
    session: AsyncSession, overrides: dict[str, Any]
) -> None:
    await assert_refused(session, LlmCallRow, call_values(**overrides))


@pytest.mark.parametrize(
    "overrides",
    [{"requests_used": -1}, {"tokens_used": -1}],
    ids=["negative-requests", "negative-tokens"],
)
async def test_negative_budget_counter_is_refused(
    session: AsyncSession, overrides: dict[str, Any]
) -> None:
    values = {"provider": "gemini", "day": date(2026, 11, 7), "requests_used": 1, "tokens_used": 0}

    await assert_refused(session, LlmBudgetUsageRow, values | overrides)


async def test_budget_usage_is_one_row_per_provider_and_day(session: AsyncSession) -> None:
    values = {"provider": "groq", "day": date(2026, 11, 7), "requests_used": 1, "tokens_used": 0}
    await session.execute(insert(LlmBudgetUsageRow).values(**values))

    await assert_refused(session, LlmBudgetUsageRow, values)
    await session.execute(insert(LlmBudgetUsageRow).values(**values | {"provider": "gemini"}))
    await session.execute(insert(LlmBudgetUsageRow).values(**values | {"day": date(2026, 11, 8)}))


async def seed_calls(session: AsyncSession) -> str:
    """5,000 rows over 50 conversations plus calls with no conversation, then fresh statistics."""
    await session.execute(
        text(
            """
            INSERT INTO llm_calls (id, created_at, conversation_id, role, provider, model, status,
                                   latency_ms)
            SELECT gen_random_uuid(),
                   timestamptz '2026-01-01 00:00+00' + n * interval '1 minute',
                   CASE WHEN n % 2 = 0
                        THEN ('00000000-0000-0000-0000-' || lpad((n % 50)::text, 12, '0'))::uuid
                   END,
                   'agent', 'gemini', 'gemini-3.5-flash-lite', 'ok', 100
            FROM generate_series(1, 5000) AS n
            """
        )
    )
    await session.execute(text("ANALYZE llm_calls"))
    return "00000000-0000-0000-0000-000000000004"


async def plan_of(session: AsyncSession, query: str, **parameters: Any) -> str:
    result = await session.execute(text(f"EXPLAIN {query}"), parameters)
    return "\n".join(row[0] for row in result)


async def test_cost_per_conversation_uses_the_partial_index(session: AsyncSession) -> None:
    conversation = await seed_calls(session)

    plan = await plan_of(
        session,
        "SELECT sum(equivalent_cost_usd_micros) FROM llm_calls "
        "WHERE conversation_id = CAST(:conversation AS uuid)",
        conversation=conversation,
    )

    assert "ix_llm_calls_conversation_id_created_at" in plan, plan


async def test_filter_by_created_at_uses_its_index(session: AsyncSession) -> None:
    await seed_calls(session)

    plan = await plan_of(
        session,
        "SELECT count(*) FROM llm_calls WHERE created_at >= timestamptz '2026-01-04 02:00+00'",
    )

    assert "ix_llm_calls_created_at" in plan, plan
