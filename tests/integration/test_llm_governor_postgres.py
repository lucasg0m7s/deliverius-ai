"""The governor on the real store: rows, counters and the budget under parallel calls."""

import asyncio
from typing import Any

import pytest
from langchain_core.messages import HumanMessage
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from support.llm_rows import GEMINI_REF, NOW, SAMPLE_USAGE, TODAY

from deliverius.llm.errors import LlmBudgetExceededError
from deliverius.llm.fake import Fail, Say, ScriptedChatModel
from deliverius.llm.governor import UsageGovernor
from deliverius.llm.ports import BudgetLimit
from deliverius.llm.repository import PostgresLlmUsageStore
from deliverius.llm.roles import ModelRole
from deliverius.shared.clock import FixedClock

pytestmark = pytest.mark.integration

CUSTOMER_TEXT = "CUSTOMER-TEXT-5d2e"


def governor_on(factory: async_sessionmaker[AsyncSession], limit: BudgetLimit) -> UsageGovernor:
    return UsageGovernor(
        role=ModelRole.AGENT,
        model=GEMINI_REF,
        limit=limit,
        store=PostgresLlmUsageStore(factory),
        clock=FixedClock(NOW),
    )


async def rows(factory: async_sessionmaker[AsyncSession], query: str) -> list[dict[str, Any]]:
    async with factory() as session:
        return [dict(row._mapping) for row in await session.execute(text(query))]


async def test_a_success_and_a_failure_leave_their_rows_and_the_counters(
    llm_usage_factory: async_sessionmaker[AsyncSession],
) -> None:
    governor = governor_on(llm_usage_factory, BudgetLimit(100, 100_000))
    model = ScriptedChatModel(
        [
            Say(f"reply to {CUSTOMER_TEXT}", SAMPLE_USAGE),
            Fail(RuntimeError(f"echo {CUSTOMER_TEXT}")),
        ],
        callbacks=[governor],
    )

    await model.ainvoke([HumanMessage(CUSTOMER_TEXT)])
    with pytest.raises(RuntimeError):
        await model.ainvoke([HumanMessage(CUSTOMER_TEXT)])

    calls = await rows(llm_usage_factory, "SELECT * FROM llm_calls ORDER BY status")
    assert [(c["status"], c["error_type"]) for c in calls] == [
        ("error", "RuntimeError"),
        ("ok", None),
    ]
    ok = calls[1]
    assert (ok["input_tokens"], ok["output_tokens"], ok["reasoning_tokens"]) == (1_000, 200, 50)
    assert (ok["real_cost_usd_micros"], ok["equivalent_cost_usd_micros"]) == (0, 800)
    assert calls[0]["input_tokens"] is None
    assert CUSTOMER_TEXT not in str(calls)
    [usage] = await rows(llm_usage_factory, "SELECT * FROM llm_budget_usage")
    assert (usage["provider"], usage["day"], usage["requests_used"], usage["tokens_used"]) == (
        "gemini",
        TODAY,
        2,
        1_200,
    )


async def test_fifty_parallel_calls_with_ten_requests_left_reach_the_model_exactly_ten_times(
    llm_usage_factory: async_sessionmaker[AsyncSession],
) -> None:
    governor = governor_on(llm_usage_factory, BudgetLimit(daily_requests=10, daily_tokens=None))
    model = ScriptedChatModel([Say("ok", SAMPLE_USAGE) for _ in range(50)], callbacks=[governor])

    results = await asyncio.gather(
        *(model.ainvoke([HumanMessage("hello")]) for _ in range(50)), return_exceptions=True
    )

    refused = [r for r in results if isinstance(r, LlmBudgetExceededError)]
    assert len(refused) == 40
    assert model.calls_made == 10
    [usage] = await rows(llm_usage_factory, "SELECT * FROM llm_budget_usage")
    assert usage["requests_used"] == 10
    assert usage["tokens_used"] == 10 * 1_200
    assert len(await rows(llm_usage_factory, "SELECT id FROM llm_calls")) == 10
