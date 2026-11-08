"""The message generation on the real LLM runtime, with providers that answer by the prompt.

A scripted model answers in call order, and parallel calls have no order. `PromptDrivenModel`
answers by what the human message says, after an optional delay, so a test can make the responses
arrive out of order and still know which scenario each one belongs to.
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, override

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.messages.ai import UsageMetadata
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import BaseModel, PrivateAttr
from support.llm_rows import NOW
from support.llm_runtime import settings_with

from deliverius.config import Settings
from deliverius.datagen.generation import ModelsForAttempt, cached_models
from deliverius.datagen.messages import ROLE_OF
from deliverius.datagen.placeholders import render_marker
from deliverius.datagen.scenarios import MessageScenario, MessageSet
from deliverius.llm.factory import ModelBuilder, ModelSpec
from deliverius.llm.fake import Fail, Say, ScriptedChatModel, Step, Structured
from deliverius.llm.in_memory import InMemoryLlmUsageStore
from deliverius.llm.roles import ModelRole
from deliverius.llm.runtime import LlmRuntime, RetryPolicy
from deliverius.shared.clock import FixedClock
from deliverius.shared.model_ref import LlmProvider

FAST_RETRY = RetryPolicy(attempts=1, initial_seconds=0.0, max_seconds=0.0, jitter_seconds=0.0)
USAGE = UsageMetadata(input_tokens=1_000, output_tokens=200, total_tokens=1_200)

type Answer = tuple[float, Step]
type Responder = Callable[[str], Answer]


def after(step: Step, delay: float = 0.0) -> Answer:
    return delay, step


def structured(value: BaseModel | dict[str, Any], delay: float = 0.0) -> Answer:
    return delay, Structured(value)


@dataclass
class Tracker:
    """What a provider saw: the prompts, and the most calls it had open at the same time."""

    prompts: list[tuple[str, str]] = field(default_factory=list)
    in_flight: int = 0
    max_in_flight: int = 0

    def start(self, system: str, human: str) -> None:
        self.prompts.append((system, human))
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)

    def finish(self) -> None:
        self.in_flight -= 1

    @property
    def calls(self) -> int:
        return len(self.prompts)


class PromptDrivenModel(ScriptedChatModel):
    """Answers each call by its prompt. Counts calls and how many ran at once."""

    _respond: Responder = PrivateAttr()
    _tracker: Tracker = PrivateAttr()

    def __init__(self, respond: Responder, tracker: Tracker, **kwargs: Any) -> None:
        super().__init__([], **kwargs)
        self._respond = respond
        self._tracker = tracker

    @override
    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        system, human = str(messages[0].content), str(messages[-1].content)
        self._position += 1
        self._tracker.start(system, human)
        try:
            delay, step = self._respond(human)
            if delay:
                await asyncio.sleep(delay)
        finally:
            self._tracker.finish()
        return ChatResult(generations=[ChatGeneration(message=_message_for(step, kwargs))])


def _message_for(step: Step, kwargs: dict[str, Any]) -> AIMessage:
    match step:
        case Structured(value=value):
            args = value.model_dump(mode="json") if isinstance(value, BaseModel) else dict(value)
            name = kwargs["tools"][0]["function"]["name"]
            return AIMessage(
                content="",
                tool_calls=[{"name": name, "args": args, "id": "call-1"}],
                usage_metadata=USAGE,
            )
        case Say(text=text):
            return AIMessage(content=text, usage_metadata=USAGE)
        case Fail(error=error):
            raise error
        case _:
            raise AssertionError(step)


@dataclass
class PromptDrivenProvider:
    respond: Responder
    tracker: Tracker = field(default_factory=Tracker)

    def __call__(self, spec: ModelSpec) -> PromptDrivenModel:
        return PromptDrivenModel(
            self.respond, self.tracker, callbacks=spec.callbacks, cache=spec.cache_setting
        )


@dataclass
class DatagenHarness:
    runtime: LlmRuntime
    store: InMemoryLlmUsageStore
    gemini: PromptDrivenProvider
    groq: PromptDrivenProvider
    cache_path: Path
    clock: FixedClock

    def models(self, message_set: MessageSet = MessageSet.MAIN) -> ModelsForAttempt:
        return cached_models(self.runtime, ROLE_OF[message_set], self.cache_path)

    def uncached_models(self, role: ModelRole = ModelRole.DATAGEN) -> ModelsForAttempt:
        return lambda attempt: self.runtime.models_for(role)

    def providers_used(self) -> set[LlmProvider]:
        return {call.provider for call in self.store.calls}


def harness(
    cache_path: Path,
    gemini: Responder,
    groq: Responder | None = None,
    settings: Settings | None = None,
    store: InMemoryLlmUsageStore | None = None,
) -> DatagenHarness:
    gemini_provider = PromptDrivenProvider(gemini)
    groq_provider = PromptDrivenProvider(groq or (lambda _: after(Say("no groq in this test"))))
    builders: dict[LlmProvider, ModelBuilder] = {
        LlmProvider.GEMINI: gemini_provider,
        LlmProvider.GROQ: groq_provider,
    }
    store = store if store is not None else InMemoryLlmUsageStore()
    clock = FixedClock(NOW)
    runtime = LlmRuntime(
        settings or settings_with(), store, clock, builders=builders, retry=FAST_RETRY
    )
    return DatagenHarness(runtime, store, gemini_provider, groq_provider, cache_path, clock)


def valid_variations(scenario: MessageScenario, count: int = 10) -> list[str]:
    """Distinct texts that carry every marker the scenario asks for, and nothing else."""
    markers = " ".join(render_marker(marker) for marker in scenario.markers)
    return [f"Mensagem {n} do cenário {scenario.id} {markers}".strip() for n in range(count)]
