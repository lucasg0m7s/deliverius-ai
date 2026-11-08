from datetime import date

from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deliverius.db.errors import translate_db_errors
from deliverius.db.tables import LlmBudgetUsageRow, LlmCallRow
from deliverius.llm.ports import BudgetLimit, BudgetUsage, LlmCallRecord
from deliverius.shared.errors import InvariantError
from deliverius.shared.model_ref import LlmProvider

# One statement, so there is no read followed by a write: the row stays locked only while it runs.
# The INSERT path refuses a zero limit and the conflict path refuses a reached one; a NULL limit
# means unlimited. No row comes back when the call is refused.
RESERVE = text(
    """
    INSERT INTO llm_budget_usage (provider, day, requests_used, tokens_used)
    SELECT CAST(:provider AS text), CAST(:day AS date), 1, 0
    WHERE (CAST(:max_requests AS integer) IS NULL OR CAST(:max_requests AS integer) > 0)
      AND (CAST(:max_tokens AS bigint) IS NULL OR CAST(:max_tokens AS bigint) > 0)
    ON CONFLICT (provider, day) DO UPDATE
      SET requests_used = llm_budget_usage.requests_used + 1
      WHERE (CAST(:max_requests AS integer) IS NULL
             OR llm_budget_usage.requests_used < CAST(:max_requests AS integer))
        AND (CAST(:max_tokens AS bigint) IS NULL
             OR llm_budget_usage.tokens_used < CAST(:max_tokens AS bigint))
    RETURNING requests_used
    """
)

ADD_TOKENS = text(
    """
    INSERT INTO llm_budget_usage (provider, day, requests_used, tokens_used)
    VALUES (CAST(:provider AS text), CAST(:day AS date), 0, CAST(:tokens AS bigint))
    ON CONFLICT (provider, day) DO UPDATE
      SET tokens_used = llm_budget_usage.tokens_used + CAST(:tokens AS bigint)
    RETURNING tokens_used
    """
)


class PostgresLlmUsageStore:
    """`LlmUsageStore` on Postgres. Each call opens, commits and closes its own transaction."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def reserve(self, provider: LlmProvider, day: date, limit: BudgetLimit) -> int | None:
        parameters = {
            "provider": provider.value,
            "day": day,
            "max_requests": limit.daily_requests,
            "max_tokens": limit.daily_tokens,
        }
        with translate_db_errors():
            async with self._session_factory() as session, session.begin():
                reserved: int | None = (
                    await session.execute(RESERVE, parameters)
                ).scalar_one_or_none()
        return reserved

    async def add_tokens(self, provider: LlmProvider, day: date, tokens: int) -> int:
        if tokens < 0:
            raise InvariantError("llm_usage.negative_tokens")
        parameters = {"provider": provider.value, "day": day, "tokens": tokens}
        with translate_db_errors():
            async with self._session_factory() as session, session.begin():
                total: int = (await session.execute(ADD_TOKENS, parameters)).scalar_one()
        return total

    async def read_usage(self, provider: LlmProvider, day: date) -> BudgetUsage:
        with translate_db_errors():
            async with self._session_factory() as session:
                row = await session.get(LlmBudgetUsageRow, (provider.value, day))
        return BudgetUsage(0, 0) if row is None else BudgetUsage(row.requests_used, row.tokens_used)

    async def record_call(self, record: LlmCallRecord) -> None:
        values = {
            "id": record.id,
            "created_at": record.created_at,
            "conversation_id": record.conversation_id,
            "sandbox_id": record.sandbox_id,
            "role": record.role.value,
            "provider": record.provider.value,
            "model": record.model,
            "prompt_name": record.prompt_name,
            "prompt_version": record.prompt_version,
            "prompt_hash": record.prompt_hash,
            "status": record.status.value,
            "error_type": record.error_type,
            "latency_ms": record.latency_ms,
            "input_tokens": record.usage.input,
            "output_tokens": record.usage.output,
            "reasoning_tokens": record.usage.reasoning,
            "real_cost_usd_micros": record.real_cost_usd_micros,
            "equivalent_cost_usd_micros": record.equivalent_cost_usd_micros,
            "trace_id": record.trace_id,
        }
        with translate_db_errors():
            async with self._session_factory() as session, session.begin():
                await session.execute(insert(LlmCallRow).values(**values))
