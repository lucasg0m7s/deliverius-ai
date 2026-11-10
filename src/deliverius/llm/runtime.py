"""The LLM layer assembled: models per role, with budget, pause, rate limit, retries and fallback.

Callers hold no transaction while a model runs (`robustez.md`, section 4): the budget reservation
and the usage log are their own short transactions, before and after the call.
"""

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC
from typing import Any, Self

import httpx
import structlog
from langchain_core.caches import BaseCache
from langchain_core.exceptions import (
    ModelAPIError,
    ModelConnectionError,
    ModelRateLimitError,
    ModelTimeoutError,
)
from langchain_core.language_models import BaseChatModel, LanguageModelInput
from langchain_core.messages import BaseMessage
from langchain_core.rate_limiters import BaseRateLimiter, InMemoryRateLimiter
from langchain_core.runnables import Runnable, RunnableConfig, RunnableLambda
from langchain_core.runnables.retry import ExponentialJitterParams
from pydantic import BaseModel, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deliverius.config import Settings
from deliverius.llm.errors import LlmConfigurationError
from deliverius.llm.factory import ModelBuilder, ModelSpec, default_builders
from deliverius.llm.gate import ProviderGate, Sleep
from deliverius.llm.governor import UsageGovernor
from deliverius.llm.ports import BudgetLimit, BudgetStatus, LlmUsageStore
from deliverius.llm.repository import PostgresLlmUsageStore
from deliverius.llm.roles import ROLE_CONFIGS, ModelRole
from deliverius.shared.clock import Clock
from deliverius.shared.model_ref import LlmProvider, ModelRef

logger = structlog.get_logger(__name__)

# The error classes `langchain_core` marks `is_retryable`; the other `ModelError`s (bad key,
# bad request, refusal) are not worth repeating and go straight to the fallback.
# The raw `httpx` errors are there because `langchain_google_genai` lets timeouts and connection
# failures through untranslated. `httpx.TransportError` would be too broad: it also covers
# `UnsupportedProtocol` and `ProxyError`, which are configuration errors that repeating cannot fix.
RETRYABLE_ERRORS = (
    ModelRateLimitError,
    ModelAPIError,
    ModelConnectionError,
    ModelTimeoutError,
    httpx.TimeoutException,
    httpx.NetworkError,
)

API_KEY_VARIABLES = {
    LlmProvider.GEMINI: "GEMINI_API_KEY",
    LlmProvider.GROQ: "GROQ_API_KEY",
    LlmProvider.ANTHROPIC: "ANTHROPIC_API_KEY",
}


def per_minute_limiter(requests_per_minute: int) -> InMemoryRateLimiter:
    """At most `requests_per_minute` in any minute, with half of it free for a burst right away.

    The library's bucket starts empty and holds one request, so it spaces every call by 60/RPM
    seconds (6 s at 10 RPM), and one conversation makes several calls in a row. A full bucket of
    half the limit, refilled at the other half, takes a turn's burst at once without exceeding it.
    """
    burst = max(requests_per_minute // 2, 1)
    refill_per_minute = max(requests_per_minute - burst, 1)
    limiter = InMemoryRateLimiter(requests_per_second=refill_per_minute / 60, max_bucket_size=burst)
    limiter.available_tokens = burst
    return limiter


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Up to 2 repeats (3 attempts), with exponential wait and jitter."""

    attempts: int = 3
    initial_seconds: float = 1.0
    max_seconds: float = 8.0
    jitter_seconds: float = 1.0

    def jitter_params(self) -> ExponentialJitterParams:
        return {
            "initial": self.initial_seconds,
            "max": self.max_seconds,
            "jitter": self.jitter_seconds,
        }


@dataclass(frozen=True, slots=True)
class RoleModels:
    """The two models of a role: the agent passes both to its middleware, the rest `runnable()`."""

    role: ModelRole
    primary: BaseChatModel
    primary_ref: ModelRef
    fallback: BaseChatModel | None
    fallback_ref: ModelRef | None
    retry: RetryPolicy = RetryPolicy()

    def runnable(self) -> Runnable[LanguageModelInput, BaseMessage]:
        """Retries on transient errors, then falls back. Each attempt goes through the governor."""
        return self._resilient(self.primary, self.fallback)

    def structured_runnable(
        self, schema: type[BaseModel]
    ) -> Runnable[LanguageModelInput, dict[str, Any] | BaseModel]:
        """Like `runnable()`, but each answer is parsed into `schema`.

        The parsing is part of each model, before the retry and the fallback: an answer that does
        not fit the schema is not repeated (only transient errors are), and the reserve gets its
        chance to answer. Whoever calls it must check the type of what comes back.
        """
        return self._resilient(
            self.primary.with_structured_output(schema),
            None if self.fallback is None else self.fallback.with_structured_output(schema),
        )

    def _resilient[Output](
        self,
        primary: Runnable[LanguageModelInput, Output],
        fallback: Runnable[LanguageModelInput, Output] | None,
    ) -> Runnable[LanguageModelInput, Output]:
        retried = self._with_retry(primary)
        if fallback is None:
            return retried
        return retried.with_fallbacks([RunnableLambda(self._logged(self._with_retry(fallback)))])

    def _with_retry[Output](
        self, model: Runnable[LanguageModelInput, Output]
    ) -> Runnable[LanguageModelInput, Output]:
        return model.with_retry(
            retry_if_exception_type=RETRYABLE_ERRORS,
            wait_exponential_jitter=True,
            exponential_jitter_params=self.retry.jitter_params(),
            stop_after_attempt=self.retry.attempts,
        )

    def _logged[Output](
        self, fallback: Runnable[LanguageModelInput, Output]
    ) -> Callable[[LanguageModelInput, RunnableConfig], Awaitable[Output]]:
        async def call(messages: LanguageModelInput, config: RunnableConfig) -> Output:
            logger.warning(
                "llm.fallback_used",
                role=self.role,
                from_provider=self.primary_ref.provider,
                to_provider=self.fallback_ref.provider if self.fallback_ref else None,
            )
            return await fallback.ainvoke(messages, config)

        return call


class LlmRuntime:
    """Builds and keeps the models, the rate limiters and the pauses, one of each per provider."""

    def __init__(
        self,
        settings: Settings,
        store: LlmUsageStore,
        clock: Clock,
        *,
        roles: Iterable[ModelRole] = tuple(ModelRole),
        builders: Mapping[LlmProvider, ModelBuilder] | None = None,
        retry: RetryPolicy | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._settings = settings
        self._store = store
        self._clock = clock
        self._retry = retry or RetryPolicy()
        self._sleep = sleep
        # An injected builder (the scripted fake) replaces the provider's integration, and so
        # its key requirement too.
        self._injected = frozenset(builders or {})
        self._builders = {**default_builders(settings.ollama_base_url), **(builders or {})}
        self._gates: dict[LlmProvider, ProviderGate] = {}
        self._limiters: dict[LlmProvider, BaseRateLimiter | None] = {}
        self._models: dict[ModelRole, RoleModels] = {}
        self._require_keys(tuple(roles))

    @classmethod
    def build(
        cls,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Clock,
        *,
        roles: Iterable[ModelRole] = tuple(ModelRole),
    ) -> Self:
        """The production assembly, on Postgres. `roles` limits which keys are required.

        Fails with `LlmConfigurationError` if a role in use lacks the key of its provider.
        """
        return cls(settings, PostgresLlmUsageStore(session_factory), clock, roles=roles)

    def models_for(self, role: ModelRole, *, cache: BaseCache | None = None) -> RoleModels:
        """Models of a role. Without `cache` they are built once and reused, with no cache at all.

        A cache is for evals and data generation only: whoever asks passes the object, and the
        governor then counts the budget after the call instead of refusing before it.
        """
        if cache is None and role in self._models:
            return self._models[role]
        primary_ref, fallback_ref = self._references(role)
        models = RoleModels(
            role=role,
            primary=self._model(role, primary_ref, cache),
            primary_ref=primary_ref,
            fallback=None if fallback_ref is None else self._model(role, fallback_ref, cache),
            fallback_ref=fallback_ref,
            retry=self._retry,
        )
        if cache is None:
            self._models[role] = models
        return models

    async def budget_status(self, role: ModelRole) -> BudgetStatus:
        """Today's (UTC) use of the provider of the role's main model, against our ceilings.

        For callers that must stop on their own because a model with a cache only counts the
        budget. Raises `DatabaseUnavailableError` if the store is down.
        """
        provider = self._references(role)[0].provider
        day = self._clock.now().astimezone(UTC).date()
        limits = self._settings.limits_for(provider)
        return BudgetStatus(
            provider=provider,
            day=day,
            usage=await self._store.read_usage(provider, day),
            limit=BudgetLimit(limits.daily_requests, limits.daily_tokens),
        )

    def pause_seconds_left(self, role: ModelRole) -> float:
        """How long the provider of the role's main model still asks to be left alone; `0.0` if not.

        For callers that wait out a short pause instead of giving up on `PROVIDER_PAUSED`.
        """
        return self.gate_for(self._references(role)[0].provider).seconds_left()

    def rate_limiter_for(self, provider: LlmProvider) -> BaseRateLimiter | None:
        """The one limiter of a provider, shared by every role; `None` when it has no limit."""
        if provider not in self._limiters:
            rpm = self._settings.limits_for(provider).requests_per_minute
            self._limiters[provider] = None if rpm is None else per_minute_limiter(rpm)
        return self._limiters[provider]

    def gate_for(self, provider: LlmProvider) -> ProviderGate:
        if provider not in self._gates:
            self._gates[provider] = ProviderGate(provider, self._clock, self._sleep)
        return self._gates[provider]

    def _model(self, role: ModelRole, ref: ModelRef, cache: BaseCache | None) -> BaseChatModel:
        limits = self._settings.limits_for(ref.provider)
        governor = UsageGovernor(
            role=role,
            model=ref,
            limit=BudgetLimit(limits.daily_requests, limits.daily_tokens),
            store=self._store,
            clock=self._clock,
            paid=ref.provider in self._settings.llm_paid_providers,
            pause=self.gate_for(ref.provider),
            counts_only=cache is not None,
        )
        spec = ModelSpec(
            role=role,
            ref=ref,
            config=ROLE_CONFIGS[role],
            api_key=self._api_key(ref.provider),
            rate_limiter=self.rate_limiter_for(ref.provider),
            cache=cache,
            callbacks=[governor],
        )
        return self._builders[ref.provider](spec)

    def _api_key(self, provider: LlmProvider) -> SecretStr | None:
        match provider:
            case LlmProvider.GEMINI:
                return self._settings.gemini_api_key
            case LlmProvider.GROQ:
                return self._settings.groq_api_key
            case LlmProvider.ANTHROPIC:
                return self._settings.anthropic_api_key
            case LlmProvider.OLLAMA:
                return None

    def _references(self, role: ModelRole) -> tuple[ModelRef, ModelRef | None]:
        """Main model and fallback. Only the agent and the writer have a fallback."""
        settings = self._settings
        references: dict[ModelRole, tuple[ModelRef, ModelRef | None]] = {
            ModelRole.AGENT: (settings.llm_agent_model, settings.llm_agent_fallback),
            ModelRole.WRITER: (settings.llm_writer_model, settings.llm_writer_fallback),
            ModelRole.CUSTOMER_SIM: (settings.llm_customer_sim_model, None),
            ModelRole.JUDGE: (settings.llm_judge_model, None),
            ModelRole.DATAGEN: (settings.llm_datagen_model, None),
            ModelRole.DATAGEN_HOLDOUT: (settings.llm_datagen_holdout_model, None),
            ModelRole.INTENT_BASELINE: (settings.llm_intent_baseline_model, None),
        }
        return references[role]

    def _require_keys(self, roles: tuple[ModelRole, ...]) -> None:
        missing: dict[str, list[str]] = {}
        for role in roles:
            primary, fallback = self._references(role)
            for suffix, ref in (("MODEL", primary), ("FALLBACK", fallback)):
                key_variable = None if ref is None else self._missing_key_variable(ref.provider)
                if key_variable is not None:
                    missing.setdefault(key_variable, []).append(f"LLM_{role.name}_{suffix}")
        if missing:
            raise LlmConfigurationError(missing)

    def _missing_key_variable(self, provider: LlmProvider) -> str | None:
        variable = API_KEY_VARIABLES.get(provider)
        if variable is None or provider in self._injected or self._api_key(provider) is not None:
            return None
        return variable
