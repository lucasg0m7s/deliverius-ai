"""Retries, fallback and degraded mode, through the real runtime with scripted providers."""

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest
from langchain_core.exceptions import ModelAPIError, ModelAuthenticationError
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableLambda
from structlog.testing import capture_logs
from support.llm_doubles import StoreDown
from support.llm_rows import NOW, SAMPLE_USAGE
from support.llm_runtime import ScriptedProvider, scripted_runtime, settings_with

from deliverius.llm.call import DegradedMode, DegradedReason, LlmReply, call_or_degrade
from deliverius.llm.fake import Fail, Hang, Say, Step
from deliverius.llm.governor import call_metadata
from deliverius.llm.in_memory import InMemoryLlmUsageStore
from deliverius.llm.ports import CallStatus
from deliverius.llm.prompts import Prompt
from deliverius.llm.roles import ModelRole
from deliverius.llm.runtime import RoleModels
from deliverius.shared.clock import FixedClock
from deliverius.shared.model_ref import LlmProvider

QUESTION = [HumanMessage("Where is my order?")]
TODAY = NOW.date()


@dataclass
class Setup:
    models: RoleModels
    store: InMemoryLlmUsageStore
    clock: FixedClock
    gemini: ScriptedProvider
    groq: ScriptedProvider


def setup(
    gemini: list[Step],
    groq: list[Step],
    role: ModelRole = ModelRole.WRITER,
    store: InMemoryLlmUsageStore | None = None,
    **settings: Any,
) -> Setup:
    gemini_provider, groq_provider = ScriptedProvider([gemini]), ScriptedProvider([groq])
    providers = {LlmProvider.GEMINI: gemini_provider, LlmProvider.GROQ: groq_provider}
    runtime, store, clock = scripted_runtime(settings_with(**settings), providers, store)
    return Setup(runtime.models_for(role), store, clock, gemini_provider, groq_provider)


def calls_of(provider: ScriptedProvider) -> int:
    return sum(model.calls_made for model in provider.built)


async def test_a_transient_error_is_retried_and_both_attempts_are_recorded() -> None:
    s = setup([Fail(ModelAPIError("boom")), Say("recovered", SAMPLE_USAGE)], [])

    result = await call_or_degrade(s.models.runnable(), QUESTION, ModelRole.WRITER)

    assert isinstance(result, LlmReply)
    assert result.message.content == "recovered"
    assert calls_of(s.gemini) == 2
    assert [c.status for c in s.store.calls] == [CallStatus.ERROR, CallStatus.OK]
    assert [c.error_type for c in s.store.calls] == ["ModelAPIError", None]
    assert calls_of(s.groq) == 0
    usage = s.store.usage(LlmProvider.GEMINI, TODAY)
    assert usage is not None
    assert usage.requests_used == 2


async def test_an_error_that_is_not_worth_repeating_goes_straight_to_the_fallback() -> None:
    s = setup([Fail(ModelAuthenticationError("bad key"))], [Say("from the reserve", SAMPLE_USAGE)])

    with capture_logs() as logs:
        result = await call_or_degrade(s.models.runnable(), QUESTION, ModelRole.WRITER)

    assert isinstance(result, LlmReply)
    assert result.message.content == "from the reserve"
    assert calls_of(s.gemini) == 1
    [event] = [entry for entry in logs if entry["event"] == "llm.fallback_used"]
    assert (event["from_provider"], event["to_provider"]) == (LlmProvider.GEMINI, LlmProvider.GROQ)
    assert [(c.provider, c.status) for c in s.store.calls] == [
        (LlmProvider.GEMINI, CallStatus.ERROR),
        (LlmProvider.GROQ, CallStatus.OK),
    ]


async def test_retries_run_out_after_three_attempts_and_the_fallback_answers() -> None:
    gemini: list[Step] = [Fail(ModelAPIError("boom")) for _ in range(5)]
    s = setup(gemini, [Say("from the reserve", SAMPLE_USAGE)])

    result = await call_or_degrade(s.models.runnable(), QUESTION, ModelRole.WRITER)

    assert isinstance(result, LlmReply)
    assert calls_of(s.gemini) == 3
    assert len(s.store.calls) == 4
    usage = s.store.usage(LlmProvider.GEMINI, TODAY)
    assert usage is not None
    assert usage.requests_used == 3


async def test_the_fallback_is_retried_too() -> None:
    groq: list[Step] = [Fail(ModelAPIError()), Say("second try", SAMPLE_USAGE)]
    s = setup([Fail(ModelAuthenticationError("bad key"))], groq)

    result = await call_or_degrade(s.models.runnable(), QUESTION, ModelRole.WRITER)

    assert isinstance(result, LlmReply)
    assert calls_of(s.groq) == 2


async def test_a_role_without_a_fallback_degrades_after_its_retries() -> None:
    groq: list[Step] = [Fail(ModelAPIError("boom")) for _ in range(3)]
    s = setup([], groq, role=ModelRole.JUDGE)

    with capture_logs() as logs:
        result = await call_or_degrade(s.models.runnable(), QUESTION, ModelRole.JUDGE)

    assert result == DegradedMode(DegradedReason.PROVIDER_ERROR)
    assert calls_of(s.groq) == 3
    assert not [entry for entry in logs if entry["event"] == "llm.fallback_used"]


async def test_when_every_provider_fails_the_result_is_degraded_with_the_reason_logged() -> None:
    gemini: list[Step] = [Fail(ModelAuthenticationError("secret provider text"))]
    s = setup(gemini, [Fail(ModelAuthenticationError("more secret text"))])

    with capture_logs() as logs:
        result = await call_or_degrade(s.models.runnable(), QUESTION, ModelRole.WRITER)

    assert result == DegradedMode(DegradedReason.PROVIDER_ERROR)
    [event] = [entry for entry in logs if entry["event"] == "llm.degraded"]
    assert event["reason"] == "provider_error"
    assert event["error_type"] == "ModelAuthenticationError"
    assert "secret" not in str(logs)


async def test_a_primary_without_budget_is_not_called_and_the_fallback_answers() -> None:
    s = setup([Say("never")], [Say("from the reserve", SAMPLE_USAGE)], llm_gemini_daily_requests=0)

    result = await call_or_degrade(s.models.runnable(), QUESTION, ModelRole.WRITER)

    assert isinstance(result, LlmReply)
    assert result.message.content == "from the reserve"
    assert calls_of(s.gemini) == 0


async def test_with_no_budget_anywhere_no_model_is_called_and_the_result_is_degraded() -> None:
    s = setup(
        [Say("never")],
        [Say("never")],
        llm_gemini_daily_requests=0,
        llm_groq_daily_requests=0,
    )

    result = await call_or_degrade(s.models.runnable(), QUESTION, ModelRole.WRITER)

    assert result == DegradedMode(DegradedReason.BUDGET_EXHAUSTED)
    assert calls_of(s.gemini) == calls_of(s.groq) == 0
    assert s.store.calls == []


async def test_with_the_budget_database_down_the_result_is_degraded_and_no_model_is_called() -> (
    None
):
    s = setup([Say("never")], [Say("never")], store=StoreDown())

    result = await call_or_degrade(s.models.runnable(), QUESTION, ModelRole.WRITER)

    assert result == DegradedMode(DegradedReason.BUDGET_UNAVAILABLE)
    assert calls_of(s.gemini) == calls_of(s.groq) == 0


async def test_a_model_that_hangs_ends_in_degraded_mode_at_the_total_deadline() -> None:
    s = setup([Hang()], [Hang()])

    result = await call_or_degrade(
        s.models.runnable(), QUESTION, ModelRole.WRITER, deadline_seconds=0.05
    )

    assert result == DegradedMode(DegradedReason.DEADLINE)


async def test_cancelling_the_task_keeps_propagating_instead_of_degrading() -> None:
    s = setup([Hang()], [Hang()])
    task = asyncio.create_task(call_or_degrade(s.models.runnable(), QUESTION, ModelRole.WRITER))
    for _ in range(10):
        await asyncio.sleep(0)

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


async def test_a_provider_timeout_inside_the_deadline_is_not_reported_as_the_deadline() -> None:
    s = setup([Fail(TimeoutError("provider"))], [Fail(TimeoutError("provider"))])

    result = await call_or_degrade(s.models.runnable(), QUESTION, ModelRole.WRITER)

    assert result == DegradedMode(DegradedReason.PROVIDER_ERROR)


async def test_an_answer_that_is_not_a_message_is_degraded() -> None:
    result = await call_or_degrade(
        RunnableLambda(lambda _: "plain text"), QUESTION, ModelRole.WRITER
    )

    assert result == DegradedMode(DegradedReason.PROVIDER_ERROR)


async def test_the_prompt_metadata_reaches_the_row_through_retries_and_fallback() -> None:
    s = setup([Fail(ModelAuthenticationError("bad key"))], [Say("ok", SAMPLE_USAGE)])
    prompt = Prompt("compose_reply", 3, ModelRole.WRITER, "tone", "text", "a" * 64)
    config: Any = {"metadata": call_metadata(prompt, None, None)}

    await call_or_degrade(s.models.runnable(), QUESTION, ModelRole.WRITER, config)

    assert [(c.prompt_name, c.prompt_version) for c in s.store.calls] == [
        ("compose_reply", 3),
        ("compose_reply", 3),
    ]


async def test_the_reply_is_the_message_the_model_gave() -> None:
    s = setup([Say("hello", SAMPLE_USAGE)], [])

    result = await call_or_degrade(s.models.runnable(), QUESTION, ModelRole.WRITER)

    assert isinstance(result, LlmReply)
    assert result.message.content == "hello"
    assert result.message.usage_metadata is not None
    assert result.message.usage_metadata["total_tokens"] == 1_200
