"""Helpers to assemble an `LlmRuntime` in tests: settings, in-memory store, scripted models."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from support.llm_rows import NOW

from deliverius.config import Settings
from deliverius.llm.factory import ModelBuilder, ModelSpec
from deliverius.llm.fake import ScriptedChatModel, Step
from deliverius.llm.in_memory import InMemoryLlmUsageStore
from deliverius.llm.runtime import LlmRuntime, RetryPolicy
from deliverius.shared.clock import FixedClock
from deliverius.shared.model_ref import LlmProvider

GEMINI_KEY = "gemini-key-value-a1b2"
GROQ_KEY = "groq-key-value-c3d4"
ANTHROPIC_KEY = "anthropic-key-value-e5f6"
FAST_RETRY = RetryPolicy(attempts=3, initial_seconds=0.0, max_seconds=0.0, jitter_seconds=0.0)


def settings_with(**overrides: Any) -> Settings:
    """Settings that ignore the environment file and have every provider key set."""
    values: dict[str, Any] = {
        "gemini_api_key": GEMINI_KEY,
        "groq_api_key": GROQ_KEY,
        "anthropic_api_key": ANTHROPIC_KEY,
    }
    return Settings(_env_file=None, **(values | overrides))  # type: ignore[call-arg]


@dataclass
class ScriptedProvider:
    """A builder that hands every model of a provider a scripted fake with the governor attached."""

    scripts: list[Sequence[Step]]
    built: list[ScriptedChatModel] = field(default_factory=list)
    specs: list[ModelSpec] = field(default_factory=list)

    def __call__(self, spec: ModelSpec) -> ScriptedChatModel:
        script = self.scripts[len(self.built)] if len(self.built) < len(self.scripts) else []
        callbacks: list[BaseCallbackHandler] = spec.callbacks
        model = ScriptedChatModel(script, callbacks=callbacks, cache=spec.cache_setting)
        self.built.append(model)
        self.specs.append(spec)
        return model


def scripted_runtime(
    settings: Settings,
    providers: Mapping[LlmProvider, ModelBuilder],
    store: InMemoryLlmUsageStore | None = None,
    clock: FixedClock | None = None,
) -> tuple[LlmRuntime, InMemoryLlmUsageStore, FixedClock]:
    store = store if store is not None else InMemoryLlmUsageStore()
    clock = clock if clock is not None else FixedClock(NOW)
    runtime = LlmRuntime(settings, store, clock, builders=providers, retry=FAST_RETRY)
    return runtime, store, clock
