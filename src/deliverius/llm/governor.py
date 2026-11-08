"""Budget and usage log as a LangChain callback, one per model.

It sits in `callbacks=` of the real provider class instead of wrapping it, so `bind_tools`,
`with_structured_output` and the native structured output keep working. `raise_error = True` makes
a refusal in `on_chat_model_start` stop the call before it reaches the network.

Nothing here reads message text or provider error text: only the exception type, the token counts
and the metadata we put there ourselves (`call_metadata`). The log events, which become metrics:
`llm.budget_exhausted`, `llm.budget_warning`, `llm.provider_paused`, `llm.call_failed`,
`llm.usage_record_failed` and `llm.price_missing`.
"""

import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date
from typing import Any
from uuid import UUID

import structlog
from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from deliverius.db.errors import DatabaseUnavailableError
from deliverius.llm.cache import CACHE_HIT_MARKER
from deliverius.llm.errors import BudgetRefusal, LlmBudgetExceededError, ProviderPausedError
from deliverius.llm.ports import (
    UNLIMITED,
    BudgetLimit,
    CallStatus,
    LlmCallRecord,
    LlmUsageStore,
    ProviderPause,
)
from deliverius.llm.pricing import PRICES, TokenUsage, equivalent_cost_usd_micros
from deliverius.llm.prompts import Prompt
from deliverius.llm.roles import ModelRole
from deliverius.shared.clock import Clock
from deliverius.shared.errors import InvariantError
from deliverius.shared.ids import new_id
from deliverius.shared.model_ref import ModelRef

logger = structlog.get_logger(__name__)

# A cancelled call fires no end or error event, so its entry would stay forever without a bound.
OPEN_RUN_LIMIT = 1_024
WARNING_PERCENT = 80

PROMPT_NAME = "prompt_name"
PROMPT_VERSION = "prompt_version"
PROMPT_HASH = "prompt_hash"
CONVERSATION_ID = "conversation_id"
SANDBOX_ID = "sandbox_id"


def call_metadata(
    prompt: Prompt | None, conversation_id: UUID | None, sandbox_id: UUID | None
) -> dict[str, str | int]:
    """The `metadata` of the call's `config`: how the governor learns which prompt and who."""
    metadata: dict[str, str | int] = {}
    if prompt is not None:
        metadata |= {
            PROMPT_NAME: prompt.name,
            PROMPT_VERSION: prompt.version,
            PROMPT_HASH: prompt.content_hash,
        }
    if conversation_id is not None:
        metadata[CONVERSATION_ID] = str(conversation_id)
    if sandbox_id is not None:
        metadata[SANDBOX_ID] = str(sandbox_id)
    return metadata


@dataclass(frozen=True, slots=True)
class _CallContext:
    conversation_id: UUID | None = None
    sandbox_id: UUID | None = None
    prompt_name: str | None = None
    prompt_version: int | None = None
    prompt_hash: str | None = None


@dataclass(frozen=True, slots=True)
class _OpenRun:
    started_at: float
    day: date
    context: _CallContext


class UsageGovernor(AsyncCallbackHandler):
    """Reserves budget before a call and records its usage and cost after it.

    With `counts_only` (a model with a response cache), nothing is refused before the call, since a
    cache hit is only known afterwards; a real call then counts against the budget when it ends.
    """

    raise_error = True

    def __init__(
        self,
        *,
        role: ModelRole,
        model: ModelRef,
        limit: BudgetLimit,
        store: LlmUsageStore,
        clock: Clock,
        paid: bool = False,
        pause: ProviderPause | None = None,
        counts_only: bool = False,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._role = role
        self._model = model
        self._limit = limit
        self._store = store
        self._clock = clock
        self._paid = paid
        self._pause = pause
        self._counts_only = counts_only
        self._monotonic = monotonic
        self._runs: OrderedDict[UUID, _OpenRun] = OrderedDict()

    @property
    def open_runs(self) -> int:
        return len(self._runs)

    async def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[BaseMessage]],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        await self._wait_if_paused()
        day = self._clock.now().astimezone(UTC).date()
        if not self._counts_only:
            await self._reserve(day)
        self._open(run_id, _OpenRun(self._monotonic(), day, _context_from(metadata)))

    async def on_llm_end(self, response: LLMResult, *, run_id: UUID, **kwargs: Any) -> None:
        run = self._runs.pop(run_id, None)
        usage, cache_hit = _inspect(response)
        if cache_hit:
            return
        if run is None:
            self._log_unknown_run()
            return
        await self._settle(run, CallStatus.OK, usage, error_type=None)

    async def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        run = self._runs.pop(run_id, None)
        if run is None:
            return
        error_type = type(error).__name__
        logger.warning(
            "llm.call_failed", provider=self._model.provider, role=self._role, error_type=error_type
        )
        await self._settle(run, CallStatus.ERROR, TokenUsage(None, None), error_type=error_type)
        if self._pause is not None:
            self._pause.note_error(error)

    async def _wait_if_paused(self) -> None:
        if self._pause is None:
            return
        try:
            await self._pause.wait_if_paused()
        except ProviderPausedError as error:
            logger.warning(
                "llm.provider_paused",
                provider=self._model.provider,
                state="refused",
                seconds_left=error.seconds_left,
            )
            raise

    async def _reserve(self, day: date) -> None:
        provider = self._model.provider
        try:
            used = await self._store.reserve(provider, day, self._limit)
        except DatabaseUnavailableError as error:
            logger.error(  # noqa: TRY400 - the error text may carry driver details
                "llm.budget_exhausted", provider=provider, reason=BudgetRefusal.DATABASE_UNAVAILABLE
            )
            raise LlmBudgetExceededError(provider, BudgetRefusal.DATABASE_UNAVAILABLE) from error
        if used is None:
            logger.warning(
                "llm.budget_exhausted", provider=provider, reason=BudgetRefusal.EXHAUSTED
            )
            raise LlmBudgetExceededError(provider, BudgetRefusal.EXHAUSTED)
        self._warn_if_requests_cross(used)

    def _open(self, run_id: UUID, run: _OpenRun) -> None:
        while len(self._runs) >= OPEN_RUN_LIMIT:
            self._runs.popitem(last=False)
        self._runs[run_id] = run

    async def _settle(
        self, run: _OpenRun, status: CallStatus, usage: TokenUsage, *, error_type: str | None
    ) -> None:
        """Count, then log. A failure in any step is logged and never reaches the caller."""
        provider = self._model.provider
        if self._counts_only:
            await self._attempt("request", lambda: self._count_request(run.day))
        spent = (usage.input or 0) + (usage.output or 0)
        if spent > 0:
            await self._attempt("tokens", lambda: self._add_tokens(run.day, spent))
        equivalent = self._equivalent_cost(usage)
        record = LlmCallRecord(
            id=new_id(),
            created_at=self._clock.now(),
            role=self._role,
            provider=provider,
            model=self._model.model,
            status=status,
            latency_ms=max(0, round((self._monotonic() - run.started_at) * 1000)),
            usage=usage,
            conversation_id=run.context.conversation_id,
            sandbox_id=run.context.sandbox_id,
            prompt_name=run.context.prompt_name,
            prompt_version=run.context.prompt_version,
            prompt_hash=run.context.prompt_hash,
            error_type=error_type,
            real_cost_usd_micros=equivalent if self._paid else 0,
            equivalent_cost_usd_micros=equivalent,
        )
        await self._attempt("call", lambda: self._store.record_call(record))

    def _equivalent_cost(self, usage: TokenUsage) -> int | None:
        if self._model not in PRICES:
            logger.warning(
                "llm.price_missing", provider=self._model.provider, model=self._model.model
            )
        return equivalent_cost_usd_micros(self._model, usage)

    async def _count_request(self, day: date) -> None:
        used = await self._store.reserve(self._model.provider, day, UNLIMITED)
        if used is not None:
            self._warn_if_requests_cross(used)

    async def _add_tokens(self, day: date, tokens: int) -> None:
        total = await self._store.add_tokens(self._model.provider, day, tokens)
        limit = self._limit.daily_tokens
        if limit is not None and total - tokens < _warning_threshold(limit) <= total:
            self._warn("tokens", total, limit)

    async def _attempt(self, step: str, action: Callable[[], Awaitable[object]]) -> None:
        try:
            await action()
        except Exception as error:
            logger.error(  # noqa: TRY400 - the error text may carry driver details
                "llm.usage_record_failed",
                provider=self._model.provider,
                step=step,
                error_type=type(error).__name__,
            )

    def _warn_if_requests_cross(self, used: int) -> None:
        limit = self._limit.daily_requests
        if limit is not None and used == _warning_threshold(limit):
            self._warn("requests", used, limit)

    def _warn(self, kind: str, used: int, limit: int) -> None:
        logger.warning(
            "llm.budget_warning", provider=self._model.provider, kind=kind, used=used, limit=limit
        )

    def _log_unknown_run(self) -> None:
        logger.error(
            "llm.usage_record_failed",
            provider=self._model.provider,
            step="call",
            error_type="UnknownRun",
        )


def _warning_threshold(limit: int) -> int:
    """The first count at or above 80% of the limit: the one call that crosses it warns, once."""
    return -(-limit * WARNING_PERCENT // 100)


def _context_from(metadata: dict[str, Any] | None) -> _CallContext:
    values = metadata or {}
    return _CallContext(
        conversation_id=_as_uuid(values.get(CONVERSATION_ID)),
        sandbox_id=_as_uuid(values.get(SANDBOX_ID)),
        prompt_name=_as_text(values.get(PROMPT_NAME)),
        prompt_version=_as_int(values.get(PROMPT_VERSION)),
        prompt_hash=_as_text(values.get(PROMPT_HASH)),
    )


def _as_uuid(value: object) -> UUID | None:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError:
            return None
    return None


def _as_text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _as_int(value: object) -> int | None:
    return value if type(value) is int else None


def _inspect(response: LLMResult) -> tuple[TokenUsage, bool]:
    """Token counts as the provider reported them, and whether the answer came from the cache."""
    for generations in response.generations:
        for generation in generations:
            if isinstance(generation, ChatGeneration) and isinstance(generation.message, AIMessage):
                message = generation.message
                if message.response_metadata.get(CACHE_HIT_MARKER):
                    return TokenUsage(None, None), True
                return _usage_of(message), False
    return TokenUsage(None, None), False


def _usage_of(message: AIMessage) -> TokenUsage:
    metadata = message.usage_metadata
    if metadata is None:
        return TokenUsage(None, None)
    details = metadata.get("output_token_details") or {}
    try:
        return TokenUsage(
            input=metadata["input_tokens"],
            output=metadata["output_tokens"],
            reasoning=details.get("reasoning"),
        )
    except InvariantError:
        # A provider reporting nonsense must not turn a good answer into an error.
        return TokenUsage(None, None)
