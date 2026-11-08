"""What the governor must never do: leak content, or leave state behind when a call is cancelled."""

import asyncio
import itertools
import logging
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, LLMResult
from structlog.testing import capture_logs
from support.llm_doubles import RecordingFails
from support.llm_rows import GEMINI_REF, NOW, SAMPLE_USAGE, TODAY

from deliverius.llm.fake import Fail, Hang, Say, ScriptedChatModel
from deliverius.llm.governor import OPEN_RUN_LIMIT, UsageGovernor
from deliverius.llm.in_memory import InMemoryLlmUsageStore
from deliverius.llm.ports import BudgetLimit, BudgetUsage
from deliverius.llm.pricing import TokenUsage
from deliverius.llm.roles import ModelRole
from deliverius.shared.clock import FixedClock
from deliverius.shared.ids import new_id
from deliverius.shared.model_ref import LlmProvider, ModelRef

DAY = TODAY
GEMINI = GEMINI_REF
START = NOW
USAGE = SAMPLE_USAGE
USER_TEXT = "TEXT-OF-THE-CUSTOMER-7f3a"
PROVIDER_ERROR_TEXT = "PROVIDER-ECHOED-9c1d"
REPLY_TEXT = "REPLY-OF-THE-MODEL-4b8e"


def build_governor(
    store: InMemoryLlmUsageStore, limit: BudgetLimit | None = None, model: ModelRef = GEMINI
) -> UsageGovernor:
    ticks = itertools.count()
    return UsageGovernor(
        role=ModelRole.AGENT,
        model=model,
        limit=limit or BudgetLimit(None, None),
        store=store,
        clock=FixedClock(START),
        monotonic=lambda: float(next(ticks)),
    )


def mentions_secret(texts: list[str]) -> bool:
    secrets = (USER_TEXT, PROVIDER_ERROR_TEXT, REPLY_TEXT)
    return any(secret in text for text in texts for secret in secrets)


class ContentCase:
    def __init__(self, name: str, store: InMemoryLlmUsageStore, step: Any) -> None:
        self.name = name
        self.store = store
        self.step = step


CASES = {
    "success": lambda: ContentCase(
        "success", InMemoryLlmUsageStore(), Say(f"{REPLY_TEXT} {USER_TEXT}", USAGE)
    ),
    "provider-error": lambda: ContentCase(
        "provider-error",
        InMemoryLlmUsageStore(),
        Fail(RuntimeError(f"{PROVIDER_ERROR_TEXT} repeated {USER_TEXT}")),
    ),
    "recording-fails": lambda: ContentCase(
        "recording-fails",
        RecordingFails(f"{PROVIDER_ERROR_TEXT} {USER_TEXT}"),
        Say(REPLY_TEXT, USAGE),
    ),
}


@pytest.mark.parametrize("case_name", list(CASES))
async def test_text_of_the_message_the_reply_or_the_provider_error_is_in_no_row_and_no_log(
    case_name: str, caplog: pytest.LogCaptureFixture
) -> None:
    case = CASES[case_name]()
    governor = build_governor(case.store)
    model = ScriptedChatModel([case.step], callbacks=[governor])

    with capture_logs() as logs, caplog.at_level(logging.DEBUG):
        try:
            await model.ainvoke([HumanMessage(USER_TEXT)])
        except RuntimeError:
            assert case_name == "provider-error"

    assert case.store.calls or case_name == "recording-fails"
    assert not mentions_secret([repr(call) for call in case.store.calls])
    assert not mentions_secret([str(entry) for entry in logs])
    assert not mentions_secret([record.getMessage() for record in caplog.records])
    assert not mentions_secret([str(case.store.usage(LlmProvider.GEMINI, DAY))])


async def test_a_cancelled_call_leaves_no_row_but_keeps_its_reservation() -> None:
    store = InMemoryLlmUsageStore()
    governor = build_governor(store)
    model = ScriptedChatModel([Hang()], callbacks=[governor])
    task = asyncio.create_task(model.ainvoke([HumanMessage("hello")]))
    for _ in range(10):
        await asyncio.sleep(0)
    assert model.calls_made == 1

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.calls == []
    assert store.usage(LlmProvider.GEMINI, DAY) == BudgetUsage(1, 0)
    assert governor.open_runs == 1


@pytest.mark.parametrize(
    ("starts", "kept"),
    [
        (OPEN_RUN_LIMIT - 1, OPEN_RUN_LIMIT - 1),
        (OPEN_RUN_LIMIT, OPEN_RUN_LIMIT),
        (OPEN_RUN_LIMIT + 1, OPEN_RUN_LIMIT),
    ],
)
async def test_open_runs_never_pass_the_limit(starts: int, kept: int) -> None:
    governor = build_governor(InMemoryLlmUsageStore())

    for _ in range(starts):
        await governor.on_chat_model_start({}, [[HumanMessage("x")]], run_id=new_id())

    assert governor.open_runs == kept


async def test_the_oldest_open_run_is_the_one_dropped() -> None:
    store = InMemoryLlmUsageStore()
    governor = build_governor(store)
    run_ids = [new_id() for _ in range(OPEN_RUN_LIMIT + 1)]
    for run_id in run_ids:
        await governor.on_chat_model_start({}, [[HumanMessage("x")]], run_id=run_id)
    ended = LLMResult(generations=[[ChatGeneration(message=AIMessage(content="done"))]])

    await governor.on_llm_end(ended, run_id=run_ids[0])
    assert store.calls == []

    await governor.on_llm_end(ended, run_id=run_ids[-1])
    assert len(store.calls) == 1
    assert governor.open_runs == OPEN_RUN_LIMIT - 1


async def test_finished_calls_leave_nothing_in_the_registry() -> None:
    governor = build_governor(InMemoryLlmUsageStore())
    model = ScriptedChatModel(
        [Say("ok", TokenUsage(1, 1)), Fail(ConnectionError())], callbacks=[governor]
    )

    await model.ainvoke([HumanMessage("x")])
    with pytest.raises(ConnectionError):
        await model.ainvoke([HumanMessage("x")])

    assert governor.open_runs == 0


def test_a_synchronous_invoke_bypasses_the_async_governor_and_the_call_goes_ahead_unrecorded() -> (
    None
):
    """Measured, and it contradicts the design: LangChain swallows async-handler errors on `invoke`.

    `_run_coros` logs "Error in callback coroutine" and carries on whatever `raise_error` says, so a
    refusal does not stop the call. The application is async only (`ainvoke`), where the refusal
    does raise; this test pins the sync behavior so a change in either direction is noticed.
    """
    store = InMemoryLlmUsageStore()
    governor = build_governor(store, limit=BudgetLimit(daily_requests=0, daily_tokens=None))
    model = ScriptedChatModel([Say(REPLY_TEXT, USAGE)], callbacks=[governor])

    with capture_logs() as logs:
        reply = model.invoke([HumanMessage("hello")])

    assert reply.content == REPLY_TEXT
    assert model.calls_made == 1
    assert store.calls == []
    assert store.usage(LlmProvider.GEMINI, DAY) is None
    assert [entry["event"] for entry in logs] == ["llm.budget_exhausted", "llm.usage_record_failed"]
    assert logs[1]["error_type"] == "UnknownRun"
