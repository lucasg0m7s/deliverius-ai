"""Builds one chat model per role and provider, with the role's limits and the governor attached.

Provider SDK retries are off (`max_retries`): the only retry layer is `with_retry` in
`RoleModels.runnable()`, so the budget sees every request that leaves. Network destinations come
from configuration or from constants here, never from a client or from the model's output.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import partial
from typing import Any

from langchain_anthropic import ChatAnthropic
from langchain_core.caches import BaseCache
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.rate_limiters import BaseRateLimiter
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from deliverius.llm.roles import ModelRole, RoleConfig
from deliverius.shared.model_ref import LlmProvider, ModelRef

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
GEMINI_THINKING_LEVEL = "minimal"
# Groq's gpt-oss reasons at "medium" by default. Reasoning counts against the output ceiling and the
# tokens-per-minute quota, and production roles keep it low for latency, as with Gemini's level.
# Ollama gets none: not every local model accepts the parameter.
GROQ_REASONING_EFFORT = "low"
# The OpenAI client refuses an empty key, and a local Ollama server ignores it.
OLLAMA_PLACEHOLDER_KEY = SecretStr("ollama")
# Gemini treats 0 as "SDK default" (5 retries); 1 is the value that means a single request.
GEMINI_SINGLE_ATTEMPT = 1


@dataclass(frozen=True, slots=True)
class ModelSpec:
    """Everything a provider integration needs to build the model of one role."""

    role: ModelRole
    ref: ModelRef
    config: RoleConfig
    api_key: SecretStr | None
    rate_limiter: BaseRateLimiter | None
    # `None` is written as an explicit `cache=False`, so no global cache can reach the model.
    cache: BaseCache | None
    callbacks: list[BaseCallbackHandler]

    @property
    def cache_setting(self) -> BaseCache | bool:
        return False if self.cache is None else self.cache


type ModelBuilder = Callable[[ModelSpec], BaseChatModel]


def gemini_model(spec: ModelSpec) -> BaseChatModel:
    return ChatGoogleGenerativeAI(
        model=spec.ref.model,
        api_key=spec.api_key,
        temperature=spec.config.temperature,
        max_tokens=spec.config.max_output_tokens,
        timeout=spec.config.attempt_timeout_seconds,
        max_retries=GEMINI_SINGLE_ATTEMPT,
        thinking_level=GEMINI_THINKING_LEVEL,
        rate_limiter=spec.rate_limiter,
        cache=spec.cache_setting,
        callbacks=spec.callbacks,
    )


def openai_compatible_model(
    spec: ModelSpec,
    *,
    base_url: str,
    reasoning_effort: str | None = None,
    http_async_client: Any = None,
) -> BaseChatModel:
    """Groq and Ollama: one integration, two destinations. `http_async_client` is for tests."""
    return ChatOpenAI(
        model=spec.ref.model,
        api_key=spec.api_key or OLLAMA_PLACEHOLDER_KEY,
        base_url=base_url,
        temperature=spec.config.temperature,
        max_completion_tokens=spec.config.max_output_tokens,
        reasoning_effort=reasoning_effort,
        timeout=spec.config.attempt_timeout_seconds,
        max_retries=0,
        # With a custom base_url the library leaves this unset, and streaming would lose its tokens.
        stream_usage=True,
        use_responses_api=False,
        http_async_client=http_async_client,
        rate_limiter=spec.rate_limiter,
        cache=spec.cache_setting,
        callbacks=spec.callbacks,
    )


def anthropic_model(spec: ModelSpec) -> BaseChatModel:
    return ChatAnthropic(
        model=spec.ref.model,
        api_key=spec.api_key or SecretStr(""),
        temperature=spec.config.temperature,
        max_tokens=spec.config.max_output_tokens,
        timeout=spec.config.attempt_timeout_seconds,
        max_retries=0,
        rate_limiter=spec.rate_limiter,
        cache=spec.cache_setting,
        callbacks=spec.callbacks,
    )


def default_builders(ollama_base_url: str) -> Mapping[LlmProvider, ModelBuilder]:
    return {
        LlmProvider.GEMINI: gemini_model,
        LlmProvider.GROQ: partial(
            openai_compatible_model,
            base_url=GROQ_BASE_URL,
            reasoning_effort=GROQ_REASONING_EFFORT,
        ),
        LlmProvider.OLLAMA: partial(openai_compatible_model, base_url=ollama_base_url),
        LlmProvider.ANTHROPIC: anthropic_model,
    }
