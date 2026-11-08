import sqlite3
from pathlib import Path

import httpx
import pytest
from langchain_core.caches import InMemoryCache
from langchain_core.globals import set_llm_cache
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from pydantic import BaseModel, SecretStr
from structlog.testing import capture_logs
from support.llm_rows import GEMINI_REF, NOW, SAMPLE_USAGE
from support.llm_runtime import ScriptedProvider, scripted_runtime, settings_with

from deliverius.llm.cache import CACHE_HIT_MARKER, SqliteResponseCache
from deliverius.llm.factory import (
    GROQ_BASE_URL,
    ModelSpec,
    anthropic_model,
    gemini_model,
    openai_compatible_model,
)
from deliverius.llm.fake import Fail, Say, ScriptedChatModel
from deliverius.llm.in_memory import InMemoryLlmUsageStore
from deliverius.llm.ports import BudgetUsage
from deliverius.llm.roles import ROLE_CONFIGS, ModelRole
from deliverius.shared.errors import InvariantError
from deliverius.shared.model_ref import LlmProvider, ModelRef

QUESTION = [HumanMessage("UNIQUE-PROMPT-TEXT-77")]
OTHER_QUESTION = [HumanMessage("another question")]
TODAY = NOW.date()


def raw_bytes(path: Path) -> bytes:
    return path.read_bytes()


def cached(script: list[object], cache: SqliteResponseCache) -> ScriptedChatModel:
    return ScriptedChatModel(script, cache=cache)  # type: ignore[arg-type]


@pytest.fixture
def cache_path(tmp_path: Path) -> Path:
    return tmp_path / "evals" / "responses.sqlite"


async def test_the_same_call_with_the_same_attempt_comes_from_the_cache(cache_path: Path) -> None:
    cache = SqliteResponseCache(cache_path, attempt=1)
    model = cached([Say("computed once", SAMPLE_USAGE)], cache)

    first = await model.ainvoke(QUESTION)
    second = await model.ainvoke(QUESTION)

    assert first.content == second.content == "computed once"
    assert model.calls_made == 1
    assert CACHE_HIT_MARKER not in first.response_metadata
    assert second.response_metadata[CACHE_HIT_MARKER] is True


async def test_the_answer_survives_a_new_cache_object_on_the_same_file(cache_path: Path) -> None:
    await cached([Say("stored")], SqliteResponseCache(cache_path, attempt=1)).ainvoke(QUESTION)
    later = cached([], SqliteResponseCache(cache_path, attempt=1))

    reply = await later.ainvoke(QUESTION)

    assert reply.content == "stored"
    assert later.calls_made == 0


async def test_another_attempt_number_is_an_independent_answer(cache_path: Path) -> None:
    first = cached([Say("attempt one")], SqliteResponseCache(cache_path, attempt=1))
    second = cached([Say("attempt two")], SqliteResponseCache(cache_path, attempt=2))

    one = await first.ainvoke(QUESTION)
    two = await second.ainvoke(QUESTION)

    assert (one.content, two.content) == ("attempt one", "attempt two")
    assert (await first.ainvoke(QUESTION)).content == "attempt one"
    assert (await second.ainvoke(QUESTION)).content == "attempt two"
    assert (first.calls_made, second.calls_made) == (1, 1)


async def test_another_message_is_not_a_hit(cache_path: Path) -> None:
    model = cached([Say("one"), Say("two")], SqliteResponseCache(cache_path, attempt=1))

    await model.ainvoke(QUESTION)
    reply = await model.ainvoke(OTHER_QUESTION)

    assert reply.content == "two"
    assert model.calls_made == 2


async def test_another_tool_list_is_not_a_hit(cache_path: Path) -> None:
    @tool
    def get_order(order_id: str) -> str:
        """Fetch an order."""
        return order_id

    @tool
    def get_customer(customer_id: str) -> str:
        """Fetch a customer."""
        return customer_id

    model = cached([Say("with order"), Say("with customer")], SqliteResponseCache(cache_path, 1))

    first = await model.bind_tools([get_order]).ainvoke(QUESTION)
    second = await model.bind_tools([get_customer]).ainvoke(QUESTION)
    again = await model.bind_tools([get_order]).ainvoke(QUESTION)

    assert (first.content, second.content, again.content) == (
        "with order",
        "with customer",
        "with order",
    )
    assert model.calls_made == 2


async def test_other_call_parameters_are_not_a_hit(cache_path: Path) -> None:
    model = cached([Say("cool"), Say("hot")], SqliteResponseCache(cache_path, attempt=1))

    cool = await model.bind(temperature=0.0).ainvoke(QUESTION)
    hot = await model.bind(temperature=0.9).ainvoke(QUESTION)

    assert (cool.content, hot.content) == ("cool", "hot")


async def test_a_failed_call_is_not_stored(cache_path: Path) -> None:
    cache = SqliteResponseCache(cache_path, attempt=1)
    model = cached([Fail(ConnectionError("down")), Say("recovered")], cache)

    with pytest.raises(ConnectionError):
        await model.ainvoke(QUESTION)
    reply = await model.ainvoke(QUESTION)
    again = await model.ainvoke(QUESTION)

    assert reply.content == "recovered"
    assert again.response_metadata[CACHE_HIT_MARKER] is True
    assert model.calls_made == 2


def test_the_file_and_its_folders_are_created_on_their_own(cache_path: Path) -> None:
    assert not cache_path.parent.exists()

    SqliteResponseCache(cache_path, attempt=1)

    assert cache_path.is_file()


async def test_clearing_removes_every_entry(cache_path: Path) -> None:
    cache = SqliteResponseCache(cache_path, attempt=1)
    model = cached([Say("first"), Say("second")], cache)
    await model.ainvoke(QUESTION)

    cache.clear()

    assert (await model.ainvoke(QUESTION)).content == "second"


@pytest.mark.parametrize("attempt", [0, -1])
def test_attempts_start_at_one(cache_path: Path, attempt: int) -> None:
    with pytest.raises(InvariantError) as error:
        SqliteResponseCache(cache_path, attempt=attempt)

    assert error.value.rule == "llm_cache.attempt_below_one"


async def test_the_file_holds_a_hash_of_the_request_and_not_its_text(cache_path: Path) -> None:
    await cached([Say("an answer")], SqliteResponseCache(cache_path, 1)).ainvoke(QUESTION)

    assert b"UNIQUE-PROMPT-TEXT-77" not in raw_bytes(cache_path)


async def test_a_damaged_or_foreign_entry_is_a_miss_that_the_real_answer_replaces(
    cache_path: Path,
) -> None:
    cache = SqliteResponseCache(cache_path, attempt=1)
    model = cached([Say("real one"), Say("never")], cache)
    await model.ainvoke(QUESTION)
    hostile = (
        '{"lc": 1, "type": "constructor", "id": ["langchain", "prompts", "chat", '
        '"ChatPromptTemplate"], "kwargs": {}}'
    )
    with sqlite3.connect(cache_path) as connection:
        connection.execute("UPDATE llm_responses SET value = ?", (hostile,))

    reply = await ScriptedChatModel([Say("replacement")], cache=cache).ainvoke(QUESTION)
    after = await ScriptedChatModel([], cache=cache).ainvoke(QUESTION)

    assert reply.content == "replacement"
    assert after.content == "replacement"


def spec_for(
    provider: LlmProvider, ref: ModelRef, key: str, cache: SqliteResponseCache
) -> ModelSpec:
    return ModelSpec(
        role=ModelRole.AGENT,
        ref=ref,
        config=ROLE_CONFIGS[ModelRole.AGENT],
        api_key=SecretStr(key),
        rate_limiter=None,
        cache=cache,
        callbacks=[],
    )


def completion_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "c1",
            "object": "chat.completion",
            "created": 1,
            "model": "m",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
        },
    )


async def test_the_api_key_is_not_in_the_cache_file_and_does_not_split_the_cache(
    cache_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return completion_response()

    ref = ModelRef(LlmProvider.GROQ, "openai/gpt-oss-120b")

    def model_with(key: str) -> object:
        spec = spec_for(LlmProvider.GROQ, ref, key, SqliteResponseCache(cache_path, attempt=1))
        return openai_compatible_model(
            spec,
            base_url=GROQ_BASE_URL,
            http_async_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

    first = model_with("unique-key-value-ONE-9z")
    second = model_with("unique-key-value-TWO-8y")
    await first.ainvoke(QUESTION)  # type: ignore[attr-defined]
    await second.ainvoke(QUESTION)  # type: ignore[attr-defined]

    assert len(requests) == 1
    content = raw_bytes(cache_path)
    assert b"unique-key-value" not in content


@pytest.mark.parametrize("provider", [LlmProvider.GEMINI, LlmProvider.GROQ, LlmProvider.ANTHROPIC])
def test_the_cache_key_material_never_holds_the_api_key(
    provider: LlmProvider, cache_path: Path
) -> None:
    key = "unique-key-value-ABC-123"
    ref = {
        LlmProvider.GEMINI: GEMINI_REF,
        LlmProvider.GROQ: ModelRef(LlmProvider.GROQ, "m"),
        LlmProvider.ANTHROPIC: ModelRef(LlmProvider.ANTHROPIC, "claude-x"),
    }[provider]
    spec = spec_for(provider, ref, key, SqliteResponseCache(cache_path, attempt=1))
    model = {
        LlmProvider.GEMINI: gemini_model,
        LlmProvider.GROQ: lambda s: openai_compatible_model(s, base_url=GROQ_BASE_URL),
        LlmProvider.ANTHROPIC: anthropic_model,
    }[provider](spec)

    assert key not in model._get_llm_string()


async def test_a_model_without_a_cache_calls_the_provider_every_time_despite_a_global_cache() -> (
    None
):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return completion_response()

    runtime, _, _ = scripted_runtime(
        settings_with(llm_judge_model="groq:openai/gpt-oss-120b", llm_groq_rpm=60_000),
        {
            LlmProvider.GROQ: lambda spec: openai_compatible_model(
                spec,
                base_url=GROQ_BASE_URL,
                http_async_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            )
        },
    )
    model = runtime.models_for(ModelRole.JUDGE).primary
    set_llm_cache(InMemoryCache())
    try:
        await model.ainvoke(QUESTION)
        await model.ainvoke(QUESTION)
    finally:
        set_llm_cache(None)

    assert len(requests) == 2


async def test_a_scripted_model_built_without_a_cache_also_ignores_a_global_one() -> None:
    provider = ScriptedProvider([[Say("one"), Say("two")]])
    runtime, _, _ = scripted_runtime(
        settings_with(llm_judge_model="gemini:gemini-3.5-flash-lite"),
        {LlmProvider.GEMINI: provider},
    )
    model = runtime.models_for(ModelRole.JUDGE).primary
    set_llm_cache(InMemoryCache())
    try:
        first = await model.ainvoke(QUESTION)
        second = await model.ainvoke(QUESTION)
    finally:
        set_llm_cache(None)

    assert (first.content, second.content) == ("one", "two")


class CachedRig:
    def __init__(self, cache_path: Path, **settings: object) -> None:
        self.provider = ScriptedProvider(
            [[Say("fresh one", SAMPLE_USAGE), Say("fresh two", SAMPLE_USAGE)]]
        )
        self.cache = SqliteResponseCache(cache_path, attempt=1)
        self.store = InMemoryLlmUsageStore()
        runtime, _, _ = scripted_runtime(
            settings_with(llm_judge_model="gemini:gemini-3.5-flash-lite", **settings),
            {LlmProvider.GEMINI: self.provider},
            self.store,
        )
        self.model = runtime.models_for(ModelRole.JUDGE, cache=self.cache).primary


async def test_a_cache_hit_answers_without_a_row_and_without_counting(cache_path: Path) -> None:
    rig = CachedRig(cache_path)

    first = await rig.model.ainvoke(QUESTION)
    second = await rig.model.ainvoke(QUESTION)

    assert first.content == second.content == "fresh one"
    assert len(rig.store.calls) == 1
    assert rig.store.usage(LlmProvider.GEMINI, TODAY) == BudgetUsage(1, 1_200)
    assert rig.provider.built[0].calls_made == 1


async def test_a_hit_still_answers_when_the_budget_is_already_exhausted(cache_path: Path) -> None:
    seeded = CachedRig(cache_path)
    await seeded.model.ainvoke(QUESTION)
    exhausted = CachedRig(cache_path, llm_gemini_daily_requests=0)

    reply = await exhausted.model.ainvoke(QUESTION)

    assert reply.content == "fresh one"
    assert exhausted.store.calls == []
    assert exhausted.store.usage(LlmProvider.GEMINI, TODAY) is None
    assert exhausted.provider.built[0].calls_made == 0


async def test_a_real_call_on_a_cached_model_counts_without_being_barred(cache_path: Path) -> None:
    rig = CachedRig(cache_path, llm_gemini_daily_requests=1)

    await rig.model.ainvoke(QUESTION)
    await rig.model.ainvoke(OTHER_QUESTION)

    assert rig.store.usage(LlmProvider.GEMINI, TODAY) == BudgetUsage(2, 2_400)
    assert len(rig.store.calls) == 2


async def test_a_real_call_on_a_cached_model_is_not_barred_even_with_a_zero_limit(
    cache_path: Path,
) -> None:
    rig = CachedRig(cache_path, llm_gemini_daily_requests=0, llm_gemini_daily_tokens=0)

    reply = await rig.model.ainvoke(QUESTION)

    assert reply.content == "fresh one"
    assert rig.store.usage(LlmProvider.GEMINI, TODAY) == BudgetUsage(1, 1_200)


async def test_a_hit_leaves_no_open_run_behind(cache_path: Path) -> None:
    rig = CachedRig(cache_path)
    await rig.model.ainvoke(QUESTION)
    await rig.model.ainvoke(QUESTION)

    governor = rig.model.callbacks[0]  # type: ignore[index]

    assert governor.open_runs == 0  # type: ignore[union-attr]


class Variations(BaseModel):
    variations: list[str]


def structured_response() -> httpx.Response:
    reply = '{"variations": ["first", "second"]}'
    body = completion_response().json()
    body["choices"][0]["message"]["content"] = reply
    return httpx.Response(200, json=body)


class StructuredRig:
    """A real `ChatOpenAI` on the Groq path against a simulated server that counts requests."""

    def __init__(self, cache_path: Path) -> None:
        self.cache_path = cache_path
        self.requests: list[httpx.Request] = []

    def model(self, attempt: int = 1) -> BaseChatModel:
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return structured_response()

        spec = spec_for(
            LlmProvider.GROQ,
            ModelRef(LlmProvider.GROQ, "openai/gpt-oss-120b"),
            "any-key",
            SqliteResponseCache(self.cache_path, attempt=attempt),
        )
        return openai_compatible_model(
            spec,
            base_url=GROQ_BASE_URL,
            http_async_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )


async def test_a_structured_answer_from_the_openai_client_is_reused_by_a_new_cache_object(
    cache_path: Path,
) -> None:
    rig = StructuredRig(cache_path)

    first = await rig.model().with_structured_output(Variations).ainvoke(QUESTION)
    second = await rig.model().with_structured_output(Variations).ainvoke(QUESTION)

    assert first == second == Variations(variations=["first", "second"])
    assert isinstance(second, Variations)
    assert len(rig.requests) == 1


async def test_a_structured_cache_hit_is_marked_and_the_real_call_keeps_its_parsed_object(
    cache_path: Path,
) -> None:
    rig = StructuredRig(cache_path)

    first = await rig.model().with_structured_output(Variations, include_raw=True).ainvoke(QUESTION)
    second = (
        await rig.model().with_structured_output(Variations, include_raw=True).ainvoke(QUESTION)
    )

    assert isinstance(first["raw"].additional_kwargs["parsed"], Variations)
    assert CACHE_HIT_MARKER not in first["raw"].response_metadata
    assert second["raw"].response_metadata[CACHE_HIT_MARKER] is True
    assert second["parsing_error"] is None
    assert second["parsed"] == first["parsed"] == Variations(variations=["first", "second"])
    assert len(rig.requests) == 1


async def test_a_structured_answer_of_another_attempt_is_not_a_hit(cache_path: Path) -> None:
    rig = StructuredRig(cache_path)

    await rig.model(attempt=1).with_structured_output(Variations).ainvoke(QUESTION)
    await rig.model(attempt=2).with_structured_output(Variations).ainvoke(QUESTION)

    assert len(rig.requests) == 2


async def test_an_unreadable_entry_is_logged_by_type_only_and_replaced(cache_path: Path) -> None:
    rig = StructuredRig(cache_path)
    await rig.model().with_structured_output(Variations).ainvoke(QUESTION)
    with sqlite3.connect(cache_path) as connection:
        connection.execute("UPDATE llm_responses SET value = ?", ("{SECRET-ENTRY-TEXT-55",))

    with capture_logs() as logs:
        await rig.model().with_structured_output(Variations).ainvoke(QUESTION)
    await rig.model().with_structured_output(Variations).ainvoke(QUESTION)

    [event] = [entry for entry in logs if entry["event"] == "llm.cache_entry_unreadable"]
    assert event["error_type"] == "JSONDecodeError"
    assert "SECRET-ENTRY-TEXT-55" not in str(logs)
    assert len(rig.requests) == 2
