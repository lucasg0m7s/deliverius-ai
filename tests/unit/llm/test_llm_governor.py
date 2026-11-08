import itertools
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, LLMResult
from langchain_core.tools import tool
from pydantic import BaseModel
from structlog.testing import capture_logs
from support.llm_doubles import FakePause, RecordingFails, StoreDown, TokenSumFails
from support.llm_rows import GEMINI_REF, NOW, SAMPLE_USAGE, TODAY

from deliverius.llm.errors import BudgetRefusal, LlmBudgetExceededError, ProviderPausedError
from deliverius.llm.fake import (
    CallTools,
    Fail,
    Say,
    ScriptedChatModel,
    ScriptedToolCall,
    Structured,
)
from deliverius.llm.governor import UsageGovernor, call_metadata
from deliverius.llm.in_memory import InMemoryLlmUsageStore
from deliverius.llm.ports import BudgetLimit, BudgetUsage, CallStatus, ProviderPause
from deliverius.llm.pricing import TokenUsage
from deliverius.llm.prompts import Prompt
from deliverius.llm.roles import ModelRole
from deliverius.shared.clock import FixedClock
from deliverius.shared.ids import new_id
from deliverius.shared.model_ref import LlmProvider, ModelRef

START = NOW
DAY = TODAY
GEMINI = GEMINI_REF
OLLAMA = ModelRef(LlmProvider.OLLAMA, "llama3.2:3b")
QUESTION = [HumanMessage("Where is my order?")]
USAGE = SAMPLE_USAGE
ROOMY = BudgetLimit(daily_requests=400, daily_tokens=2_000_000)
ATTEMPT_SECONDS = 0.25


@dataclass
class Rig:
    governor: UsageGovernor
    store: InMemoryLlmUsageStore
    clock: FixedClock

    def model(self, *steps: Any) -> ScriptedChatModel:
        return ScriptedChatModel(steps, callbacks=[self.governor])


def make_rig(
    *,
    model: ModelRef = GEMINI,
    limit: BudgetLimit = ROOMY,
    store: InMemoryLlmUsageStore | None = None,
    paid: bool = False,
    pause: ProviderPause | None = None,
    counts_only: bool = False,
) -> Rig:
    store = store if store is not None else InMemoryLlmUsageStore()
    clock = FixedClock(START)
    ticks = itertools.count()
    governor = UsageGovernor(
        role=ModelRole.WRITER,
        model=model,
        limit=limit,
        store=store,
        clock=clock,
        paid=paid,
        pause=pause,
        counts_only=counts_only,
        monotonic=lambda: next(ticks) * ATTEMPT_SECONDS,
    )
    return Rig(governor, store, clock)


def events(logs: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    return [entry for entry in logs if entry["event"] == name]


@tool
def get_order(order_id: str) -> str:
    """Fetch an order."""
    return order_id


class Proposal(BaseModel):
    action: str


def test_the_governor_stops_a_call_by_raising_not_by_logging() -> None:
    assert UsageGovernor.raise_error is True


async def test_a_successful_call_reserves_counts_tokens_and_records_the_row() -> None:
    rig = make_rig()

    reply = await rig.model(Say("Your order is on its way.", USAGE)).ainvoke(QUESTION)

    assert reply.content == "Your order is on its way."
    assert rig.store.usage(LlmProvider.GEMINI, DAY) == BudgetUsage(1, 1_200)
    [record] = rig.store.calls
    assert (record.role, record.provider, record.model) == (
        ModelRole.WRITER,
        LlmProvider.GEMINI,
        "gemini-3.5-flash-lite",
    )
    assert record.status is CallStatus.OK
    assert record.error_type is None
    assert record.usage == USAGE
    assert record.latency_ms == 250
    assert record.created_at == START
    assert record.equivalent_cost_usd_micros == 300 + 500
    assert record.real_cost_usd_micros == 0


async def test_a_paid_provider_has_a_real_cost_equal_to_the_list_price() -> None:
    rig = make_rig(paid=True)

    await rig.model(Say("answer", USAGE)).ainvoke(QUESTION)

    [record] = rig.store.calls
    assert record.real_cost_usd_micros == record.equivalent_cost_usd_micros == 800


async def test_a_paid_provider_without_a_price_has_no_real_cost_either() -> None:
    rig = make_rig(model=OLLAMA, paid=True)

    await rig.model(Say("answer", USAGE)).ainvoke(QUESTION)

    [record] = rig.store.calls
    assert record.real_cost_usd_micros is None
    assert record.equivalent_cost_usd_micros is None


async def test_the_prompt_and_the_owner_reach_the_row_through_the_call_config() -> None:
    rig = make_rig()
    prompt = Prompt("compose_reply", 2, ModelRole.WRITER, "tone", "text", "f" * 64)
    conversation_id, sandbox_id = new_id(), new_id()
    config = {"metadata": call_metadata(prompt, conversation_id, sandbox_id)}

    await rig.model(Say("answer", USAGE)).ainvoke(QUESTION, config=config)  # type: ignore[arg-type]

    [record] = rig.store.calls
    assert (record.prompt_name, record.prompt_version, record.prompt_hash) == (
        "compose_reply",
        2,
        "f" * 64,
    )
    assert record.conversation_id == conversation_id
    assert record.sandbox_id == sandbox_id


async def test_a_call_without_a_prompt_leaves_the_prompt_columns_empty() -> None:
    rig = make_rig()

    await rig.model(Say("answer", USAGE)).ainvoke(QUESTION)

    [record] = rig.store.calls
    assert (record.prompt_name, record.prompt_version, record.prompt_hash) == (None, None, None)
    assert (record.conversation_id, record.sandbox_id) == (None, None)


async def test_metadata_that_is_not_ours_is_ignored_instead_of_breaking_the_call() -> None:
    rig = make_rig()
    metadata = {
        "conversation_id": "not-a-uuid",
        "sandbox_id": 42,
        "prompt_name": ["x"],
        "prompt_version": "3",
        "prompt_hash": None,
        "ls_provider": "anything",
    }

    await rig.model(Say("answer", USAGE)).ainvoke(QUESTION, config={"metadata": metadata})

    [record] = rig.store.calls
    assert (record.conversation_id, record.sandbox_id, record.prompt_name) == (None, None, None)
    assert (record.prompt_version, record.prompt_hash) == (None, None)


def test_call_metadata_holds_only_what_was_given() -> None:
    assert call_metadata(None, None, None) == {}
    conversation_id = UUID(int=1)

    assert call_metadata(None, conversation_id, None) == {"conversation_id": str(conversation_id)}


async def test_a_call_over_the_request_limit_is_refused_before_reaching_the_model() -> None:
    rig = make_rig(limit=BudgetLimit(daily_requests=1, daily_tokens=None))
    model = rig.model(Say("first", USAGE), Say("never"))
    await model.ainvoke(QUESTION)

    with capture_logs() as logs, pytest.raises(LlmBudgetExceededError) as refused:
        await model.ainvoke(QUESTION)

    assert refused.value.reason is BudgetRefusal.EXHAUSTED
    assert refused.value.provider is LlmProvider.GEMINI
    assert model.calls_made == 1
    assert rig.store.usage(LlmProvider.GEMINI, DAY) == BudgetUsage(1, 1_200)
    assert len(rig.store.calls) == 1
    [event] = events(logs, "llm.budget_exhausted")
    assert event["reason"] == "exhausted"


async def test_a_call_after_the_token_limit_is_reached_is_refused() -> None:
    rig = make_rig(limit=BudgetLimit(daily_requests=100, daily_tokens=1_200))
    model = rig.model(Say("first", USAGE), Say("never"))
    await model.ainvoke(QUESTION)

    with pytest.raises(LlmBudgetExceededError):
        await model.ainvoke(QUESTION)

    assert model.calls_made == 1


async def test_a_provider_with_a_zero_limit_never_receives_a_call() -> None:
    rig = make_rig(limit=BudgetLimit(daily_requests=0, daily_tokens=0))
    model = rig.model(Say("never"))

    with pytest.raises(LlmBudgetExceededError):
        await model.ainvoke(QUESTION)

    assert model.calls_made == 0
    assert rig.store.usage(LlmProvider.GEMINI, DAY) is None


async def test_a_provider_without_a_limit_is_never_refused() -> None:
    rig = make_rig(model=OLLAMA, limit=BudgetLimit(None, None))
    model = rig.model(*(Say(str(n), USAGE) for n in range(5)))

    for _ in range(5):
        await model.ainvoke(QUESTION)

    assert rig.store.usage(LlmProvider.OLLAMA, DAY) == BudgetUsage(5, 6_000)


async def test_with_the_budget_database_down_the_call_is_refused_without_reaching_the_model() -> (
    None
):
    rig = make_rig(store=StoreDown())
    model = rig.model(Say("never"))

    with capture_logs() as logs, pytest.raises(LlmBudgetExceededError) as refused:
        await model.ainvoke(QUESTION)

    assert refused.value.reason is BudgetRefusal.DATABASE_UNAVAILABLE
    assert model.calls_made == 0
    assert events(logs, "llm.budget_exhausted")[0]["reason"] == "database_unavailable"


async def test_a_response_without_token_counts_is_returned_with_empty_usage_and_cost() -> None:
    rig = make_rig()

    reply = await rig.model(Say("no counts")).ainvoke(QUESTION)

    assert reply.content == "no counts"
    [record] = rig.store.calls
    assert record.usage == TokenUsage(None, None, None)
    assert record.equivalent_cost_usd_micros is None
    assert rig.store.usage(LlmProvider.GEMINI, DAY) == BudgetUsage(1, 0)


async def test_a_model_without_a_price_records_an_empty_cost_and_says_so() -> None:
    rig = make_rig(model=OLLAMA)

    with capture_logs() as logs:
        await rig.model(Say("answer", USAGE)).ainvoke(QUESTION)

    [record] = rig.store.calls
    assert record.equivalent_cost_usd_micros is None
    assert record.real_cost_usd_micros == 0
    assert events(logs, "llm.price_missing")[0]["model"] == "llama3.2:3b"


async def test_nonsense_token_counts_from_a_provider_do_not_break_the_answer() -> None:
    rig = make_rig()
    message = AIMessage(
        content="ok",
        usage_metadata={"input_tokens": -5, "output_tokens": 3, "total_tokens": -2},
    )
    run_id = new_id()

    await rig.governor.on_chat_model_start({}, [QUESTION], run_id=run_id)
    await rig.governor.on_llm_end(
        LLMResult(generations=[[ChatGeneration(message=message)]]), run_id=run_id
    )

    [record] = rig.store.calls
    assert record.usage == TokenUsage(None, None, None)


async def test_a_failure_writing_the_row_does_not_take_the_answer_down() -> None:
    rig = make_rig(store=RecordingFails())

    with capture_logs() as logs:
        reply = await rig.model(Say("still delivered", USAGE)).ainvoke(QUESTION)

    assert reply.content == "still delivered"
    [event] = events(logs, "llm.usage_record_failed")
    assert (event["step"], event["error_type"]) == ("call", "RuntimeError")
    assert rig.store.usage(LlmProvider.GEMINI, DAY) == BudgetUsage(1, 1_200)


async def test_a_failure_adding_tokens_is_logged_and_the_row_is_still_written() -> None:
    rig = make_rig(store=TokenSumFails())

    with capture_logs() as logs:
        reply = await rig.model(Say("delivered", USAGE)).ainvoke(QUESTION)

    assert reply.content == "delivered"
    assert [e["step"] for e in events(logs, "llm.usage_record_failed")] == ["tokens"]
    assert len(rig.store.calls) == 1


async def test_a_failed_attempt_is_recorded_with_its_error_type_and_keeps_the_reservation() -> None:
    rig = make_rig()
    model = rig.model(Fail(ConnectionError("provider down")))

    with capture_logs() as logs, pytest.raises(ConnectionError):
        await model.ainvoke(QUESTION)

    [record] = rig.store.calls
    assert record.status is CallStatus.ERROR
    assert record.error_type == "ConnectionError"
    assert record.usage == TokenUsage(None, None, None)
    assert record.equivalent_cost_usd_micros is None
    assert record.latency_ms == 250
    assert rig.store.usage(LlmProvider.GEMINI, DAY) == BudgetUsage(1, 0)
    [event] = events(logs, "llm.call_failed")
    assert event["error_type"] == "ConnectionError"
    assert "provider down" not in str(logs)


async def test_every_attempt_is_its_own_row_and_its_own_reservation() -> None:
    rig = make_rig()
    model = rig.model(Fail(ConnectionError()), Say("second try", USAGE))

    with pytest.raises(ConnectionError):
        await model.ainvoke(QUESTION)
    await model.ainvoke(QUESTION)

    assert [call.status for call in rig.store.calls] == [CallStatus.ERROR, CallStatus.OK]
    assert rig.store.usage(LlmProvider.GEMINI, DAY) == BudgetUsage(2, 1_200)


async def test_a_new_utc_day_starts_a_new_counter() -> None:
    rig = make_rig(limit=BudgetLimit(daily_requests=1, daily_tokens=None))
    rig.clock.advance(timedelta(hours=11, minutes=59, seconds=30))
    model = rig.model(Say("late", USAGE), Say("next day", USAGE))
    await model.ainvoke(QUESTION)
    with pytest.raises(LlmBudgetExceededError):
        await model.ainvoke(QUESTION)

    rig.clock.advance(timedelta(minutes=1))
    await model.ainvoke(QUESTION)

    assert rig.store.usage(LlmProvider.GEMINI, DAY) == BudgetUsage(1, 1_200)
    assert rig.store.usage(LlmProvider.GEMINI, DAY + timedelta(days=1)) == BudgetUsage(1, 1_200)


@pytest.mark.parametrize(
    ("limit", "warning_call"), [(1, 1), (2, 2), (3, 3), (5, 4), (10, 8), (400, 320)]
)
async def test_the_request_warning_comes_once_from_the_call_that_crosses_80_percent(
    limit: int, warning_call: int
) -> None:
    rig = make_rig(limit=BudgetLimit(daily_requests=limit, daily_tokens=None))
    model = rig.model(*(Say("ok", USAGE) for _ in range(limit)))

    warned_on: list[int] = []
    for call_number in range(1, limit + 1):
        with capture_logs() as logs:
            await model.ainvoke(QUESTION)
        if events(logs, "llm.budget_warning"):
            warned_on.append(call_number)

    assert warned_on == [warning_call]


async def test_the_token_warning_comes_once_from_the_sum_that_crosses_80_percent() -> None:
    rig = make_rig(limit=BudgetLimit(daily_requests=None, daily_tokens=10_000))
    amounts = [3_000, 4_000, 1_500, 500, 500]
    model = rig.model(*(Say("ok", TokenUsage(n, 0)) for n in amounts))

    warnings: list[dict[str, Any]] = []
    for _ in amounts:
        with capture_logs() as logs:
            await model.ainvoke(QUESTION)
        warnings += events(logs, "llm.budget_warning")

    assert [(w["kind"], w["used"], w["limit"]) for w in warnings] == [("tokens", 8_500, 10_000)]


async def test_the_two_kinds_of_limit_warn_independently() -> None:
    rig = make_rig(limit=BudgetLimit(daily_requests=5, daily_tokens=2_000))
    model = rig.model(*(Say("ok", TokenUsage(450, 0)) for _ in range(5)))

    with capture_logs() as logs:
        for _ in range(5):
            await model.ainvoke(QUESTION)

    assert sorted(w["kind"] for w in events(logs, "llm.budget_warning")) == ["requests", "tokens"]


async def test_a_paused_provider_refuses_the_call_before_any_reservation() -> None:
    pause = FakePause(seconds_left=60)
    rig = make_rig(pause=pause)
    model = rig.model(Say("never"))

    with capture_logs() as logs, pytest.raises(ProviderPausedError):
        await model.ainvoke(QUESTION)

    assert model.calls_made == 0
    assert rig.store.usage(LlmProvider.GEMINI, DAY) is None
    assert events(logs, "llm.provider_paused")[0]["seconds_left"] == 60


async def test_the_pause_hears_about_failures_and_not_about_successes() -> None:
    pause = FakePause()
    rig = make_rig(pause=pause)
    error = ConnectionError("down")
    model = rig.model(Say("fine", USAGE), Fail(error))

    await model.ainvoke(QUESTION)
    assert pause.errors == []
    with pytest.raises(ConnectionError):
        await model.ainvoke(QUESTION)

    assert pause.errors == [error]


async def test_binding_tools_keeps_the_governor_on_the_call() -> None:
    rig = make_rig()
    call = ScriptedToolCall("get_order", {"order_id": "o-1"})
    model = rig.model(CallTools((call,), usage=USAGE))

    reply = await model.bind_tools([get_order]).ainvoke(QUESTION)

    assert isinstance(reply, AIMessage)
    assert reply.tool_calls[0]["name"] == "get_order"
    assert len(rig.store.calls) == 1
    assert rig.store.usage(LlmProvider.GEMINI, DAY) == BudgetUsage(1, 1_200)


async def test_structured_output_keeps_the_governor_on_the_call() -> None:
    rig = make_rig(limit=BudgetLimit(daily_requests=1, daily_tokens=None))
    model = rig.model(Structured({"action": "refund"}, usage=USAGE), Say("never"))
    structured = model.with_structured_output(Proposal)

    assert await structured.ainvoke(QUESTION) == Proposal(action="refund")
    with pytest.raises(LlmBudgetExceededError):
        await structured.ainvoke(QUESTION)

    assert len(rig.store.calls) == 1
    assert model.calls_made == 1


async def test_ending_an_unknown_run_is_logged_and_writes_nothing() -> None:
    rig = make_rig()

    with capture_logs() as logs:
        await rig.governor.on_llm_end(
            LLMResult(generations=[[ChatGeneration(message=AIMessage(content="x"))]]),
            run_id=new_id(),
        )

    assert rig.store.calls == []
    assert events(logs, "llm.usage_record_failed")[0]["error_type"] == "UnknownRun"


async def test_a_failure_of_an_unknown_run_is_ignored() -> None:
    rig = make_rig()

    await rig.governor.on_llm_error(ConnectionError(), run_id=new_id())

    assert rig.store.calls == []


async def test_a_model_with_a_cache_is_not_barred_but_a_real_call_counts_afterwards() -> None:
    rig = make_rig(limit=BudgetLimit(daily_requests=1, daily_tokens=None), counts_only=True)
    model = rig.model(Say("first", USAGE), Say("second, over the limit", USAGE))

    await model.ainvoke(QUESTION)
    await model.ainvoke(QUESTION)

    assert rig.store.usage(LlmProvider.GEMINI, DAY) == BudgetUsage(2, 2_400)
    assert len(rig.store.calls) == 2


async def test_a_cache_hit_neither_counts_nor_leaves_a_row() -> None:
    rig = make_rig(counts_only=True)
    run_id = new_id()
    hit = AIMessage(content="cached", response_metadata={"llm_cache_hit": True})

    await rig.governor.on_chat_model_start({}, [QUESTION], run_id=run_id)
    await rig.governor.on_llm_end(
        LLMResult(generations=[[ChatGeneration(message=hit)]]), run_id=run_id
    )

    assert rig.store.calls == []
    assert rig.store.usage(LlmProvider.GEMINI, DAY) is None
