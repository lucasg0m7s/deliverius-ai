import pytest
from pydantic import ValidationError

from deliverius.config import ProviderLimits, Settings
from deliverius.shared.model_ref import LlmProvider, ModelRef

POSITIVE_ONLY = [
    "DATABASE_CONNECT_TIMEOUT_SECONDS",
    "DATABASE_POOL_SIZE",
    "DATABASE_POOL_TIMEOUT_SECONDS",
    "DATABASE_STATEMENT_TIMEOUT_MS",
    "DATABASE_LOCK_TIMEOUT_MS",
    "DATABASE_IDLE_IN_TRANSACTION_TIMEOUT_MS",
    "HEALTH_CHECK_TIMEOUT_SECONDS",
]


@pytest.mark.parametrize("variable", POSITIVE_ONLY)
@pytest.mark.parametrize("value", ["0", "-1"])
def test_zero_or_negative_limit_prevents_building_settings(
    monkeypatch: pytest.MonkeyPatch, variable: str, value: str
) -> None:
    monkeypatch.setenv(variable, value)

    with pytest.raises(ValidationError, match=variable.lower()):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_negative_max_overflow_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_MAX_OVERFLOW", "-1")

    with pytest.raises(ValidationError, match="database_max_overflow"):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_unknown_log_format_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_FORMAT", "xml")

    with pytest.raises(ValidationError, match="log_format"):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_defaults_are_the_documented_safe_limits() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.database_pool_size == 10
    assert settings.database_max_overflow == 5
    assert settings.database_pool_timeout_seconds == 5.0
    assert settings.database_statement_timeout_ms == 5_000
    assert settings.database_lock_timeout_ms == 2_000
    assert settings.database_idle_in_transaction_timeout_ms == 10_000
    assert settings.log_format == "json"


def build_settings() -> Settings:
    return Settings(_env_file=None)  # type: ignore[call-arg]


def test_llm_defaults_let_the_api_start_without_any_provider_key() -> None:
    settings = build_settings()

    assert settings.llm_agent_model == ModelRef(LlmProvider.GEMINI, "gemini-3.5-flash-lite")
    assert settings.llm_agent_fallback == ModelRef(LlmProvider.GROQ, "openai/gpt-oss-120b")
    assert settings.llm_judge_model.provider is LlmProvider.GROQ
    assert settings.gemini_api_key is None
    assert settings.groq_api_key is None
    assert settings.anthropic_api_key is None
    assert settings.llm_paid_providers == frozenset()


@pytest.mark.parametrize(
    ("variable", "attribute"),
    [
        ("LLM_AGENT_MODEL", "llm_agent_model"),
        ("LLM_WRITER_MODEL", "llm_writer_model"),
        ("LLM_CUSTOMER_SIM_MODEL", "llm_customer_sim_model"),
        ("LLM_JUDGE_MODEL", "llm_judge_model"),
        ("LLM_DATAGEN_MODEL", "llm_datagen_model"),
        ("LLM_DATAGEN_HOLDOUT_MODEL", "llm_datagen_holdout_model"),
        ("LLM_AGENT_FALLBACK", "llm_agent_fallback"),
        ("LLM_WRITER_FALLBACK", "llm_writer_fallback"),
    ],
)
def test_a_valid_model_reference_is_read_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, variable: str, attribute: str
) -> None:
    monkeypatch.setenv(variable, "ollama:llama3.2:3b")

    assert getattr(build_settings(), attribute) == ModelRef(LlmProvider.OLLAMA, "llama3.2:3b")


@pytest.mark.parametrize(
    "value", ["mistral:large", "gemini", "gemini:", ":model", "gemini:two words", "   "]
)
def test_an_invalid_model_reference_names_the_variable(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("LLM_WRITER_MODEL", value)

    with pytest.raises(ValidationError, match="llm_writer_model"):
        build_settings()


def test_an_invalid_fallback_reference_names_the_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_AGENT_FALLBACK", "mistral:large")

    with pytest.raises(ValidationError, match="llm_agent_fallback"):
        build_settings()


def test_a_blank_fallback_means_no_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_AGENT_FALLBACK", "")

    assert build_settings().llm_agent_fallback is None


def test_a_blank_api_key_means_not_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "")

    assert build_settings().groq_api_key is None


def test_an_api_key_never_shows_in_the_settings_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "unique-secret-value")

    settings = build_settings()

    assert settings.gemini_api_key is not None
    assert settings.gemini_api_key.get_secret_value() == "unique-secret-value"
    assert "unique-secret-value" not in repr(settings)
    assert "unique-secret-value" not in str(settings)


def test_paid_providers_are_read_as_a_comma_separated_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_PAID_PROVIDERS", "groq, anthropic")

    assert build_settings().llm_paid_providers == {LlmProvider.GROQ, LlmProvider.ANTHROPIC}


def test_an_unknown_paid_provider_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PAID_PROVIDERS", "groq,mistral")

    with pytest.raises(ValidationError, match="llm_paid_providers"):
        build_settings()


def test_default_limits_block_the_paid_provider_and_leave_ollama_unlimited() -> None:
    settings = build_settings()

    assert settings.limits_for(LlmProvider.GEMINI) == ProviderLimits(400, 2_000_000, 12)
    assert settings.limits_for(LlmProvider.GROQ) == ProviderLimits(800, 150_000, 25)
    assert settings.limits_for(LlmProvider.ANTHROPIC) == ProviderLimits(0, 0, None)
    assert settings.limits_for(LlmProvider.OLLAMA) == ProviderLimits(None, None, None)


def test_limits_can_be_overridden_per_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_OLLAMA_DAILY_REQUESTS", "50")
    monkeypatch.setenv("LLM_GROQ_RPM", "5")

    settings = build_settings()

    assert settings.limits_for(LlmProvider.OLLAMA).daily_requests == 50
    assert settings.limits_for(LlmProvider.GROQ).requests_per_minute == 5


@pytest.mark.parametrize(
    ("variable", "value"),
    [("LLM_GEMINI_DAILY_REQUESTS", "-1"), ("LLM_GROQ_DAILY_TOKENS", "-5"), ("LLM_GEMINI_RPM", "0")],
)
def test_a_negative_llm_limit_is_rejected(
    monkeypatch: pytest.MonkeyPatch, variable: str, value: str
) -> None:
    monkeypatch.setenv(variable, value)

    with pytest.raises(ValidationError, match=variable.lower()):
        build_settings()


def test_the_holdout_generator_defaults_to_another_provider_than_the_main_one() -> None:
    settings = build_settings()

    assert settings.llm_datagen_holdout_model == ModelRef(LlmProvider.GROQ, "openai/gpt-oss-120b")
    assert settings.llm_datagen_holdout_model.provider is not settings.llm_datagen_model.provider


def test_the_hugging_face_token_is_optional_and_a_blank_one_means_not_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert build_settings().hf_token is None

    monkeypatch.setenv("HF_TOKEN", "")

    assert build_settings().hf_token is None


def test_the_hugging_face_token_never_shows_in_the_settings_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HF_TOKEN", "unique-hf-token-value")

    settings = build_settings()

    assert settings.hf_token is not None
    assert settings.hf_token.get_secret_value() == "unique-hf-token-value"
    assert "unique-hf-token-value" not in repr(settings)
    assert "unique-hf-token-value" not in str(settings)
