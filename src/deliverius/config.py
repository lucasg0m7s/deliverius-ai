from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, SecretStr
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from deliverius.shared.model_ref import LlmProvider, ModelRef

type LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
type LogFormat = Literal["json", "console"]


def _split_providers(value: object) -> object:
    """Accept `groq,gemini` from the environment; a list passes through for tests and code."""
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


def _blank_as_none(value: object) -> object:
    """A variable copied from `.env.example` with no value means "not set"."""
    if isinstance(value, str) and not value.strip():
        return None
    return value


def _parse_optional_model(value: object) -> object:
    blank = _blank_as_none(value)
    return None if blank is None else ModelRef.parse(blank)


# Read as plain text, never as JSON: pydantic-settings would otherwise try to decode them.
type ModelSetting = Annotated[ModelRef, NoDecode, BeforeValidator(ModelRef.parse)]
type FallbackSetting = Annotated[ModelRef | None, NoDecode, BeforeValidator(_parse_optional_model)]
type ApiKeySetting = Annotated[SecretStr | None, BeforeValidator(_blank_as_none)]
type PaidProviders = Annotated[frozenset[LlmProvider], NoDecode, BeforeValidator(_split_providers)]


@dataclass(frozen=True, slots=True)
class ProviderLimits:
    """Our own daily and per-minute ceilings for a provider. `None` means unlimited."""

    daily_requests: int | None
    daily_tokens: int | None
    requests_per_minute: int | None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    environment: str = "development"

    database_url: str = "postgresql+asyncpg://deliverius:deliverius@localhost:5433/deliverius"
    database_connect_timeout_seconds: float = Field(default=3.0, gt=0)
    database_pool_size: int = Field(default=10, gt=0)
    database_max_overflow: int = Field(default=5, ge=0)
    database_pool_timeout_seconds: float = Field(default=5.0, gt=0)
    database_statement_timeout_ms: int = Field(default=5_000, gt=0)
    database_lock_timeout_ms: int = Field(default=2_000, gt=0)
    database_idle_in_transaction_timeout_ms: int = Field(default=10_000, gt=0)

    health_check_timeout_seconds: float = Field(default=2.0, gt=0)

    retrieval_model_dir: Path = Path("models/multilingual-e5-small")
    # Provisional until the evaluation of the search fixes the measured value.
    retrieval_min_similarity: float = Field(default=0.80, ge=0, le=1)
    retrieval_threads: int = Field(default=1, ge=1)

    log_level: LogLevel = "INFO"
    log_format: LogFormat = "json"

    llm_agent_model: ModelSetting = ModelRef(LlmProvider.GEMINI, "gemini-3.5-flash-lite")
    llm_agent_fallback: FallbackSetting = ModelRef(LlmProvider.GROQ, "openai/gpt-oss-120b")
    llm_writer_model: ModelSetting = ModelRef(LlmProvider.GEMINI, "gemini-3.5-flash-lite")
    llm_writer_fallback: FallbackSetting = ModelRef(LlmProvider.GROQ, "openai/gpt-oss-20b")
    llm_customer_sim_model: ModelSetting = ModelRef(LlmProvider.GROQ, "openai/gpt-oss-120b")
    llm_judge_model: ModelSetting = ModelRef(LlmProvider.GROQ, "openai/gpt-oss-120b")
    llm_datagen_model: ModelSetting = ModelRef(LlmProvider.GEMINI, "gemini-3.5-flash-lite")
    # Another provider than `datagen` on purpose: the holdout test measures style memorisation.
    llm_datagen_holdout_model: ModelSetting = ModelRef(LlmProvider.GROQ, "openai/gpt-oss-120b")

    # Optional: the API starts without them. Only the providers a role uses are required, and
    # that check happens when the LLM runtime is built, not here.
    gemini_api_key: ApiKeySetting = None
    groq_api_key: ApiKeySetting = None
    anthropic_api_key: ApiKeySetting = None
    ollama_base_url: str = "http://localhost:11434/v1"
    # Only for publishing the synthetic dataset; checked when publishing, never at startup.
    hf_token: ApiKeySetting = None

    llm_gemini_daily_requests: int | None = Field(default=400, ge=0)
    llm_gemini_daily_tokens: int | None = Field(default=2_000_000, ge=0)
    llm_gemini_rpm: int | None = Field(default=12, gt=0)
    llm_groq_daily_requests: int | None = Field(default=800, ge=0)
    llm_groq_daily_tokens: int | None = Field(default=150_000, ge=0)
    llm_groq_rpm: int | None = Field(default=25, gt=0)
    llm_ollama_daily_requests: int | None = Field(default=None, ge=0)
    llm_ollama_daily_tokens: int | None = Field(default=None, ge=0)
    llm_ollama_rpm: int | None = Field(default=None, gt=0)
    # Zero by default, so no real call to a paid provider happens by accident.
    llm_anthropic_daily_requests: int | None = Field(default=0, ge=0)
    llm_anthropic_daily_tokens: int | None = Field(default=0, ge=0)
    llm_anthropic_rpm: int | None = Field(default=None, gt=0)

    # Providers whose calls cost real money; every other provider is on a free quota.
    llm_paid_providers: PaidProviders = frozenset()

    def limits_for(self, provider: LlmProvider) -> ProviderLimits:
        match provider:
            case LlmProvider.GEMINI:
                return ProviderLimits(
                    self.llm_gemini_daily_requests,
                    self.llm_gemini_daily_tokens,
                    self.llm_gemini_rpm,
                )
            case LlmProvider.GROQ:
                return ProviderLimits(
                    self.llm_groq_daily_requests, self.llm_groq_daily_tokens, self.llm_groq_rpm
                )
            case LlmProvider.OLLAMA:
                return ProviderLimits(
                    self.llm_ollama_daily_requests,
                    self.llm_ollama_daily_tokens,
                    self.llm_ollama_rpm,
                )
            case LlmProvider.ANTHROPIC:
                return ProviderLimits(
                    self.llm_anthropic_daily_requests,
                    self.llm_anthropic_daily_tokens,
                    self.llm_anthropic_rpm,
                )


@lru_cache
def get_settings() -> Settings:
    return Settings()
