"""`call_structured`: a typed answer through the real runtime with scripted providers."""

from pathlib import Path

from langchain_core.exceptions import ModelAPIError
from pydantic import BaseModel
from structlog.testing import capture_logs
from support.llm_rows import NOW, SAMPLE_USAGE
from support.llm_runtime import ScriptedProvider, scripted_runtime, settings_with

from deliverius.llm.cache import SqliteResponseCache
from deliverius.llm.call import DegradedMode, DegradedReason, call_structured
from deliverius.llm.fake import Fail, Say, Step, Structured
from deliverius.llm.in_memory import InMemoryLlmUsageStore
from deliverius.llm.ports import CallStatus
from deliverius.llm.roles import ModelRole
from deliverius.llm.runtime import RoleModels
from deliverius.shared.model_ref import LlmProvider

MESSAGES = [("system", "Write variations."), ("human", "Complaint about a late order.")]
TODAY = NOW.date()


class Variations(BaseModel):
    variations: list[str]


def models_for(
    gemini: list[Step],
    groq: list[Step],
    role: ModelRole,
    cache: SqliteResponseCache | None = None,
) -> tuple[RoleModels, InMemoryLlmUsageStore, ScriptedProvider, ScriptedProvider]:
    gemini_provider, groq_provider = ScriptedProvider([gemini]), ScriptedProvider([groq])
    providers = {LlmProvider.GEMINI: gemini_provider, LlmProvider.GROQ: groq_provider}
    runtime, store, _ = scripted_runtime(settings_with(), providers)
    return runtime.models_for(role, cache=cache), store, gemini_provider, groq_provider


def calls_of(provider: ScriptedProvider) -> int:
    return sum(model.calls_made for model in provider.built)


async def test_a_valid_answer_is_an_instance_of_the_schema_and_leaves_a_usage_row() -> None:
    answer = Variations(variations=["first", "second"])
    models, store, _, _ = models_for(
        [Structured(answer, usage=SAMPLE_USAGE)], [], ModelRole.DATAGEN
    )

    result = await call_structured(models, Variations, MESSAGES, ModelRole.DATAGEN)

    assert result == answer
    [call] = store.calls
    assert (call.role, call.provider, call.status) == (
        ModelRole.DATAGEN,
        LlmProvider.GEMINI,
        CallStatus.OK,
    )
    assert call.usage == SAMPLE_USAGE
    usage = store.usage(LlmProvider.GEMINI, TODAY)
    assert usage is not None
    assert usage.requests_used == 1


async def test_plain_text_is_accepted_as_the_input() -> None:
    models, _, _, _ = models_for([Structured(Variations(variations=["a"]))], [], ModelRole.DATAGEN)

    result = await call_structured(models, Variations, "Write one variation.", ModelRole.DATAGEN)

    assert result == Variations(variations=["a"])


async def test_a_missing_field_is_invalid_output_and_the_content_never_reaches_the_log() -> None:
    models, store, gemini, _ = models_for(
        [Structured({"variations": "SECRET-MESSAGE-TEXT-31"})], [], ModelRole.DATAGEN
    )

    with capture_logs() as logs:
        result = await call_structured(models, Variations, MESSAGES, ModelRole.DATAGEN)

    assert result == DegradedMode(DegradedReason.INVALID_OUTPUT)
    assert calls_of(gemini) == 1
    [event] = [entry for entry in logs if entry["event"] == "llm.degraded"]
    assert event["reason"] == "invalid_output"
    assert event["error_type"] == "ValidationError"
    assert "SECRET-MESSAGE-TEXT-31" not in str(logs)
    assert [c.status for c in store.calls] == [CallStatus.OK]


async def test_an_answer_with_no_structured_call_is_invalid_output() -> None:
    models, _, _, _ = models_for([Say("just text")], [], ModelRole.DATAGEN)

    result = await call_structured(models, Variations, MESSAGES, ModelRole.DATAGEN)

    assert result == DegradedMode(DegradedReason.INVALID_OUTPUT)


async def test_the_reserve_answers_after_the_attempts_of_the_primary() -> None:
    answer = Variations(variations=["from the reserve"])
    gemini: list[Step] = [Fail(ModelAPIError("boom")) for _ in range(3)]
    models, store, gemini_provider, groq_provider = models_for(
        gemini, [Structured(answer, usage=SAMPLE_USAGE)], ModelRole.WRITER
    )

    with capture_logs() as logs:
        result = await call_structured(models, Variations, MESSAGES, ModelRole.WRITER)

    assert result == answer
    assert calls_of(gemini_provider) == 3
    assert calls_of(groq_provider) == 1
    assert [entry for entry in logs if entry["event"] == "llm.fallback_used"]
    assert [(c.provider, c.status) for c in store.calls][-1] == (LlmProvider.GROQ, CallStatus.OK)


async def test_an_invalid_answer_from_the_primary_is_not_repeated_and_the_reserve_answers() -> None:
    answer = Variations(variations=["valid"])
    models, _, gemini, groq = models_for(
        [Structured({"wrong": 1})], [Structured(answer)], ModelRole.WRITER
    )

    result = await call_structured(models, Variations, MESSAGES, ModelRole.WRITER)

    assert result == answer
    assert calls_of(gemini) == 1
    assert calls_of(groq) == 1


async def test_a_failure_of_every_provider_is_degraded_like_any_other_call() -> None:
    gemini: list[Step] = [Fail(ModelAPIError("boom")) for _ in range(3)]
    models, _, _, _ = models_for(gemini, [], ModelRole.DATAGEN)

    result = await call_structured(models, Variations, MESSAGES, ModelRole.DATAGEN)

    assert result == DegradedMode(DegradedReason.PROVIDER_ERROR)


async def test_a_cache_hit_does_not_count_in_the_budget(tmp_path: Path) -> None:
    cache = SqliteResponseCache(tmp_path / "cache.sqlite", attempt=1)
    answer = Variations(variations=["cached"])
    models, store, gemini, _ = models_for(
        [Structured(answer, usage=SAMPLE_USAGE)], [], ModelRole.DATAGEN, cache=cache
    )

    first = await call_structured(models, Variations, MESSAGES, ModelRole.DATAGEN)
    second = await call_structured(models, Variations, MESSAGES, ModelRole.DATAGEN)

    assert first == second == answer
    assert calls_of(gemini) == 1
    usage = store.usage(LlmProvider.GEMINI, TODAY)
    assert usage is not None
    assert usage.requests_used == 1
    assert len(store.calls) == 1
