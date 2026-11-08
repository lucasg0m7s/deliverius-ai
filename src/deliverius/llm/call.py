"""The one way to call a role outside the agent: a typed result, never a provider exception.

"No LLM" is a planned case. Its result is `DegradedMode`, and the caller decides what to do (a
fixed honest text, a handoff to a person); an LLM failure never becomes an action. A caller that
forgets the degraded case fails the exhaustive `match`, which mypy checks, instead of returning an
HTTP 500.
"""

import asyncio
from dataclasses import dataclass
from enum import StrEnum

import structlog
from langchain_core.exceptions import OutputParserException
from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import AIMessage
from langchain_core.runnables import Runnable, RunnableConfig
from pydantic import BaseModel, ValidationError

from deliverius.llm.errors import BudgetRefusal, LlmBudgetExceededError, ProviderPausedError
from deliverius.llm.roles import ROLE_CONFIGS, ModelRole
from deliverius.llm.runtime import RoleModels

logger = structlog.get_logger(__name__)


class DegradedReason(StrEnum):
    DEADLINE = "deadline"
    BUDGET_EXHAUSTED = "budget_exhausted"
    BUDGET_UNAVAILABLE = "budget_unavailable"
    PROVIDER_PAUSED = "provider_paused"
    PROVIDER_ERROR = "provider_error"
    INVALID_OUTPUT = "invalid_output"


@dataclass(frozen=True, slots=True)
class LlmReply:
    message: AIMessage


@dataclass(frozen=True, slots=True)
class DegradedMode:
    reason: DegradedReason


async def call_or_degrade(
    runnable: Runnable[LanguageModelInput, object],
    messages: LanguageModelInput,
    role: ModelRole,
    config: RunnableConfig | None = None,
    *,
    deadline_seconds: float | None = None,
) -> LlmReply | DegradedMode:
    """Runs the call within the role's total deadline and turns any failure into `DegradedMode`.

    Catches `Exception`, because each provider integration has its own. `CancelledError` is a
    `BaseException` and keeps propagating. The caller must not hold a transaction open: a model
    call takes seconds.
    """
    answer = await _run(runnable, messages, role, config, deadline_seconds)
    if isinstance(answer, DegradedMode):
        return answer
    if not isinstance(answer, AIMessage):
        return _degraded(role, DegradedReason.PROVIDER_ERROR, type(answer).__name__)
    return LlmReply(answer)


async def call_structured[T: BaseModel](
    models: RoleModels,
    schema: type[T],
    messages: LanguageModelInput,
    role: ModelRole,
    config: RunnableConfig | None = None,
    *,
    deadline_seconds: float | None = None,
) -> T | DegradedMode:
    """Like `call_or_degrade`, but the answer is an instance of `schema`.

    `messages` may be plain text or `(role, text)` pairs such as `("system", ...)` and
    `("human", ...)`, so the caller needs no LangChain type. An answer that does not fit the schema
    is `DegradedMode(INVALID_OUTPUT)`; only the exception type is logged, never the content.
    """
    answer = await _run(
        models.structured_runnable(schema), messages, role, config, deadline_seconds
    )
    if isinstance(answer, DegradedMode):
        return answer
    if not isinstance(answer, schema):
        return _degraded(role, DegradedReason.INVALID_OUTPUT, type(answer).__name__)
    return answer


async def _run(
    runnable: Runnable[LanguageModelInput, object],
    messages: LanguageModelInput,
    role: ModelRole,
    config: RunnableConfig | None,
    deadline_seconds: float | None,
) -> object | DegradedMode:
    deadline = deadline_seconds or ROLE_CONFIGS[role].total_deadline_seconds
    scope = asyncio.timeout(deadline)
    try:
        async with scope:
            return await runnable.ainvoke(messages, config)
    except Exception as error:
        reason = DegradedReason.DEADLINE if scope.expired() else _reason_for(error)
        return _degraded(role, reason, type(error).__name__)


def _degraded(role: ModelRole, reason: DegradedReason, error_type: str) -> DegradedMode:
    logger.warning("llm.degraded", role=role, reason=reason, error_type=error_type)
    return DegradedMode(reason)


def _reason_for(error: Exception) -> DegradedReason:
    if isinstance(error, LlmBudgetExceededError):
        if error.reason is BudgetRefusal.DATABASE_UNAVAILABLE:
            return DegradedReason.BUDGET_UNAVAILABLE
        return DegradedReason.BUDGET_EXHAUSTED
    if isinstance(error, ProviderPausedError):
        return DegradedReason.PROVIDER_PAUSED
    if isinstance(error, ValidationError | OutputParserException):
        return DegradedReason.INVALID_OUTPUT
    return DegradedReason.PROVIDER_ERROR
