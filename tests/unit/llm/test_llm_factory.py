import pytest
from langchain_anthropic import ChatAnthropic
from langchain_core.caches import InMemoryCache
from langchain_core.rate_limiters import InMemoryRateLimiter
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import ChatOpenAI
from sqlalchemy.ext.asyncio import create_async_engine
from support.llm_rows import NOW
from support.llm_runtime import (
    ANTHROPIC_KEY,
    GEMINI_KEY,
    GROQ_KEY,
    ScriptedProvider,
    settings_with,
)

from deliverius.db.session import create_session_factory
from deliverius.llm.errors import LlmConfigurationError
from deliverius.llm.factory import GROQ_BASE_URL
from deliverius.llm.fake import ScriptedChatModel
from deliverius.llm.governor import UsageGovernor
from deliverius.llm.in_memory import InMemoryLlmUsageStore
from deliverius.llm.roles import ROLE_CONFIGS, ModelRole
from deliverius.llm.runtime import LlmRuntime, per_minute_limiter
from deliverius.shared.clock import FixedClock
from deliverius.shared.model_ref import LlmProvider, ModelRef


def runtime_for(
    settings_overrides: dict[str, object] | None = None, **options: object
) -> LlmRuntime:
    settings = settings_with(**(settings_overrides or {}))
    return LlmRuntime(settings, InMemoryLlmUsageStore(), FixedClock(NOW), **options)  # type: ignore[arg-type]


def test_the_gemini_model_has_the_role_limits_and_a_single_sdk_attempt() -> None:
    primary = runtime_for().models_for(ModelRole.AGENT).primary
    config = ROLE_CONFIGS[ModelRole.AGENT]

    assert isinstance(primary, ChatGoogleGenerativeAI)
    assert primary.model == "gemini-3.5-flash-lite"
    assert primary.timeout == config.attempt_timeout_seconds
    assert primary.max_output_tokens == config.max_output_tokens
    assert primary.temperature == config.temperature
    assert primary.max_retries == 1
    assert primary.thinking_level == "minimal"


def test_the_groq_model_goes_through_the_openai_compatible_client_with_no_sdk_retries() -> None:
    fallback = runtime_for().models_for(ModelRole.AGENT).fallback
    config = ROLE_CONFIGS[ModelRole.AGENT]

    assert isinstance(fallback, ChatOpenAI)
    assert fallback.model_name == "openai/gpt-oss-120b"
    assert fallback.openai_api_base == GROQ_BASE_URL
    assert fallback.request_timeout == config.attempt_timeout_seconds
    assert fallback.max_tokens == config.max_output_tokens
    assert fallback.max_retries == 0
    assert fallback.stream_usage is True
    assert fallback.use_responses_api is False


def test_the_ollama_model_uses_the_configured_url_and_needs_no_key() -> None:
    runtime = runtime_for(
        {"llm_judge_model": "ollama:llama3.2:3b", "ollama_base_url": "http://box:11434/v1"}
    )

    judge = runtime.models_for(ModelRole.JUDGE).primary

    assert isinstance(judge, ChatOpenAI)
    assert judge.model_name == "llama3.2:3b"
    assert judge.openai_api_base == "http://box:11434/v1"
    assert judge.max_retries == 0


def test_the_anthropic_model_has_the_role_limits_and_no_sdk_retries() -> None:
    runtime = runtime_for({"llm_judge_model": "anthropic:claude-x"})
    config = ROLE_CONFIGS[ModelRole.JUDGE]

    judge = runtime.models_for(ModelRole.JUDGE).primary

    assert isinstance(judge, ChatAnthropic)
    assert judge.model == "claude-x"
    assert judge.default_request_timeout == config.attempt_timeout_seconds
    assert judge.max_tokens == config.max_output_tokens
    assert judge.temperature == config.temperature
    assert judge.max_retries == 0


@pytest.mark.parametrize("role", list(ModelRole))
def test_every_model_carries_its_own_governor_and_an_explicit_no_cache(role: ModelRole) -> None:
    models = runtime_for().models_for(role)

    for model in (models.primary, models.fallback):
        if model is not None:
            assert model.cache is False
            assert [type(c) for c in model.callbacks or []] == [UsageGovernor]


def test_the_two_models_of_a_role_have_different_governors() -> None:
    models = runtime_for().models_for(ModelRole.AGENT)

    assert models.primary.callbacks != models.fallback.callbacks  # type: ignore[union-attr]


def test_a_role_without_a_fallback_has_none() -> None:
    models = runtime_for().models_for(ModelRole.CUSTOMER_SIM)

    assert models.fallback is None
    assert models.fallback_ref is None


def test_models_are_built_once_per_role_when_there_is_no_cache() -> None:
    runtime = runtime_for()

    assert runtime.models_for(ModelRole.WRITER) is runtime.models_for(ModelRole.WRITER)


def test_a_cache_given_by_the_caller_is_attached_to_both_models() -> None:
    runtime = runtime_for()
    cache = InMemoryCache()

    models = runtime.models_for(ModelRole.AGENT, cache=cache)

    assert models.primary.cache is cache
    assert models.fallback is not None
    assert models.fallback.cache is cache
    assert models is not runtime.models_for(ModelRole.AGENT)
    assert runtime.models_for(ModelRole.AGENT).primary.cache is False


def test_changing_the_environment_variable_of_a_role_changes_its_model() -> None:
    runtime = runtime_for({"llm_writer_model": "groq:openai/gpt-oss-20b"})

    writer = runtime.models_for(ModelRole.WRITER).primary

    assert isinstance(writer, ChatOpenAI)
    assert writer.model_name == "openai/gpt-oss-20b"


def test_one_rate_limiter_per_provider_is_shared_by_every_role() -> None:
    runtime = runtime_for()
    agent, writer = runtime.models_for(ModelRole.AGENT), runtime.models_for(ModelRole.WRITER)

    assert isinstance(agent.primary.rate_limiter, InMemoryRateLimiter)
    assert agent.primary.rate_limiter is writer.primary.rate_limiter
    assert agent.fallback is not None
    assert writer.fallback is not None
    assert agent.fallback.rate_limiter is writer.fallback.rate_limiter
    assert agent.primary.rate_limiter is not agent.fallback.rate_limiter
    assert runtime.rate_limiter_for(LlmProvider.GEMINI) is agent.primary.rate_limiter


def test_the_rate_limiter_follows_the_configured_requests_per_minute() -> None:
    runtime = runtime_for({"llm_gemini_rpm": 30})

    limiter = runtime.rate_limiter_for(LlmProvider.GEMINI)

    assert isinstance(limiter, InMemoryRateLimiter)
    assert limiter.max_bucket_size == 15
    assert limiter.requests_per_second == 0.25


@pytest.mark.parametrize("rpm", [2, 10, 25, 31])
def test_a_minute_never_holds_more_than_the_limit(rpm: int) -> None:
    limiter = per_minute_limiter(rpm)

    assert limiter.max_bucket_size + limiter.requests_per_second * 60 == rpm


def test_the_first_burst_goes_out_without_waiting() -> None:
    limiter = per_minute_limiter(10)

    assert [limiter.acquire(blocking=False) for _ in range(6)] == [True] * 5 + [False]


def test_a_limit_of_one_still_lets_one_request_through_each_minute() -> None:
    limiter = per_minute_limiter(1)

    assert limiter.acquire(blocking=False)
    assert not limiter.acquire(blocking=False)
    assert limiter.requests_per_second == 1 / 60


def test_ollama_and_anthropic_have_no_rate_limiter_by_default() -> None:
    runtime = runtime_for()

    assert runtime.rate_limiter_for(LlmProvider.OLLAMA) is None
    assert runtime.rate_limiter_for(LlmProvider.ANTHROPIC) is None


def test_one_pause_per_provider_is_shared_by_every_role() -> None:
    runtime = runtime_for()

    assert runtime.gate_for(LlmProvider.GROQ) is runtime.gate_for(LlmProvider.GROQ)
    assert runtime.gate_for(LlmProvider.GROQ) is not runtime.gate_for(LlmProvider.GEMINI)


def test_a_missing_key_fails_the_assembly_naming_the_variable_and_not_a_value() -> None:
    settings = settings_with(groq_api_key=None)

    with pytest.raises(LlmConfigurationError) as error:
        LlmRuntime(settings, InMemoryLlmUsageStore(), FixedClock(NOW))

    message = str(error.value)
    assert "GROQ_API_KEY" in message
    assert "LLM_AGENT_FALLBACK" in message
    assert "GEMINI_API_KEY" not in message
    for value in (GEMINI_KEY, GROQ_KEY, ANTHROPIC_KEY):
        assert value not in message


def test_every_missing_key_is_reported_together() -> None:
    settings = settings_with(gemini_api_key=None, groq_api_key=None)

    with pytest.raises(LlmConfigurationError) as error:
        LlmRuntime(settings, InMemoryLlmUsageStore(), FixedClock(NOW))

    assert set(error.value.missing_keys) == {"GEMINI_API_KEY", "GROQ_API_KEY"}


def test_only_the_keys_of_the_roles_in_use_are_required() -> None:
    settings = settings_with(groq_api_key=None)

    runtime = LlmRuntime(
        settings, InMemoryLlmUsageStore(), FixedClock(NOW), roles=[ModelRole.DATAGEN]
    )

    assert isinstance(runtime.models_for(ModelRole.DATAGEN).primary, ChatGoogleGenerativeAI)


def test_the_holdout_role_needs_the_groq_key_only_when_it_is_in_use() -> None:
    settings = settings_with(groq_api_key=None)
    store, clock = InMemoryLlmUsageStore(), FixedClock(NOW)

    LlmRuntime(settings, store, clock, roles=[ModelRole.DATAGEN])
    with pytest.raises(LlmConfigurationError) as error:
        LlmRuntime(settings, store, clock, roles=[ModelRole.DATAGEN, ModelRole.DATAGEN_HOLDOUT])

    assert error.value.missing_keys == {"GROQ_API_KEY": ["LLM_DATAGEN_HOLDOUT_MODEL"]}


def test_the_holdout_role_resolves_to_its_own_model() -> None:
    runtime = runtime_for({"llm_datagen_holdout_model": "groq:openai/gpt-oss-20b"})

    models = runtime.models_for(ModelRole.DATAGEN_HOLDOUT)

    assert models.role is ModelRole.DATAGEN_HOLDOUT
    assert models.primary_ref == ModelRef(LlmProvider.GROQ, "openai/gpt-oss-20b")
    assert models.fallback is None
    assert isinstance(models.primary, ChatOpenAI)


def test_a_key_is_not_required_for_ollama() -> None:
    settings = settings_with(
        groq_api_key=None,
        gemini_api_key=None,
        llm_agent_model="ollama:a",
        llm_agent_fallback="",
        llm_writer_model="ollama:a",
        llm_writer_fallback="",
        llm_customer_sim_model="ollama:a",
        llm_judge_model="ollama:a",
        llm_datagen_model="ollama:a",
        llm_datagen_holdout_model="ollama:a",
    )

    LlmRuntime(settings, InMemoryLlmUsageStore(), FixedClock(NOW))


def test_an_injected_provider_replaces_the_integration_and_its_key_requirement() -> None:
    settings = settings_with(gemini_api_key=None, groq_api_key=None)
    fake = ScriptedProvider([[]])
    runtime = LlmRuntime(
        settings,
        InMemoryLlmUsageStore(),
        FixedClock(NOW),
        builders={LlmProvider.GEMINI: fake, LlmProvider.GROQ: ScriptedProvider([[]])},
    )

    primary = runtime.models_for(ModelRole.AGENT).primary

    assert isinstance(primary, ScriptedChatModel)
    assert [type(c) for c in primary.callbacks or []] == [UsageGovernor]
    assert fake.specs[0].role is ModelRole.AGENT
    assert fake.specs[0].api_key is None


def test_the_production_assembly_runs_on_the_postgres_store_and_checks_the_keys() -> None:
    engine = create_async_engine("postgresql+asyncpg://nobody@localhost:1/none")
    factory = create_session_factory(engine)

    runtime = LlmRuntime.build(settings_with(), factory, FixedClock(NOW))

    assert isinstance(runtime.models_for(ModelRole.AGENT).primary, ChatGoogleGenerativeAI)
    with pytest.raises(LlmConfigurationError):
        LlmRuntime.build(settings_with(gemini_api_key=None), factory, FixedClock(NOW))
    LlmRuntime.build(
        settings_with(gemini_api_key=None), factory, FixedClock(NOW), roles=[ModelRole.JUDGE]
    )
