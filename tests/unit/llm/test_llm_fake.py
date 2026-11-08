import asyncio
from typing import Any
from uuid import UUID

import pytest
from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import LLMResult
from langchain_core.tools import tool
from pydantic import BaseModel

from deliverius.llm.fake import (
    CallTools,
    Fail,
    Hang,
    Say,
    ScriptedChatModel,
    ScriptedToolCall,
    ScriptExhaustedError,
    Structured,
)
from deliverius.llm.pricing import TokenUsage

QUESTION = [HumanMessage("Where is my order?")]


class Proposal(BaseModel):
    action: str
    amount_cents: int


@tool
def get_order(order_id: str) -> str:
    """Fetch an order."""
    return order_id


class RecordingHandler(AsyncCallbackHandler):
    def __init__(self) -> None:
        self.started = 0
        self.ended: list[LLMResult] = []
        self.errors: list[BaseException] = []

    async def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[BaseMessage]],
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        self.started += 1

    async def on_llm_end(self, response: LLMResult, *, run_id: UUID, **kwargs: Any) -> None:
        self.ended.append(response)

    async def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        self.errors.append(error)


def test_the_scripted_model_is_a_regular_chat_model() -> None:
    assert isinstance(ScriptedChatModel([]), BaseChatModel)


async def test_a_text_step_answers_with_that_text() -> None:
    model = ScriptedChatModel([Say("Your order is on its way.")])

    reply = await model.ainvoke(QUESTION)

    assert reply.content == "Your order is on its way."
    assert reply.tool_calls == []


async def test_steps_are_consumed_in_order() -> None:
    model = ScriptedChatModel([Say("first"), Say("second")])

    replies = [(await model.ainvoke(QUESTION)).content for _ in range(2)]

    assert replies == ["first", "second"]
    assert model.calls_made == 2


def test_the_model_also_works_synchronously() -> None:
    model = ScriptedChatModel([Say("sync answer")])

    assert model.invoke(QUESTION).content == "sync answer"


async def test_a_scripted_tool_call_reaches_the_answer() -> None:
    call = ScriptedToolCall("get_order", {"order_id": "o-1"})
    model = ScriptedChatModel([CallTools((call,), text="Let me check.")])

    reply = await model.bind_tools([get_order]).ainvoke(QUESTION)

    assert isinstance(reply, AIMessage)
    assert reply.content == "Let me check."
    assert [(c["name"], c["args"]) for c in reply.tool_calls] == [
        ("get_order", {"order_id": "o-1"})
    ]
    assert reply.tool_calls[0]["id"]


async def test_tool_calls_get_distinct_ids_unless_the_script_names_one() -> None:
    first = ScriptedToolCall("get_order", {"order_id": "a"})
    second = ScriptedToolCall("get_order", {"order_id": "b"}, id="chosen")
    model = ScriptedChatModel([CallTools((first, second)), CallTools((first,))])
    bound = model.bind_tools([get_order])

    one = (await bound.ainvoke(QUESTION)).tool_calls
    two = (await bound.ainvoke(QUESTION)).tool_calls

    ids = [call["id"] for call in (*one, *two)]
    assert ids[1] == "chosen"
    assert len(set(ids)) == 3


async def test_binding_tools_keeps_the_position_in_the_script() -> None:
    call = ScriptedToolCall("get_order", {"order_id": "o-1"})
    model = ScriptedChatModel([Say("before binding"), CallTools((call,)), Say("after the tool")])

    assert (await model.ainvoke(QUESTION)).content == "before binding"
    bound = model.bind_tools([get_order])
    assert (await bound.ainvoke(QUESTION)).tool_calls[0]["name"] == "get_order"
    assert (await model.ainvoke(QUESTION)).content == "after the tool"
    assert model.calls_made == 3


async def test_structured_output_returns_the_validated_object() -> None:
    model = ScriptedChatModel([Structured(Proposal(action="refund", amount_cents=4590))])

    proposal = await model.with_structured_output(Proposal).ainvoke(QUESTION)

    assert proposal == Proposal(action="refund", amount_cents=4590)


async def test_structured_output_accepts_a_plain_mapping() -> None:
    model = ScriptedChatModel([Structured({"action": "coupon", "amount_cents": 1500})])

    proposal = await model.with_structured_output(Proposal).ainvoke(QUESTION)

    assert proposal == Proposal(action="coupon", amount_cents=1500)


async def test_structured_output_keeps_the_position_in_the_script() -> None:
    model = ScriptedChatModel([Say("plain"), Structured({"action": "x", "amount_cents": 1})])
    structured = model.with_structured_output(Proposal)

    assert (await model.ainvoke(QUESTION)).content == "plain"
    assert await structured.ainvoke(QUESTION) == Proposal(action="x", amount_cents=1)


async def test_an_invalid_structured_value_fails_the_schema_validation() -> None:
    model = ScriptedChatModel([Structured({"action": "refund"})])

    with pytest.raises(ValueError, match="amount_cents"):
        await model.with_structured_output(Proposal).ainvoke(QUESTION)


async def test_a_structured_step_without_a_bound_schema_says_what_is_missing() -> None:
    model = ScriptedChatModel([Structured({"action": "x", "amount_cents": 1})])

    with pytest.raises(ValueError, match="exactly one bound schema"):
        await model.ainvoke(QUESTION)


async def test_a_structured_step_can_name_the_tool_explicitly() -> None:
    model = ScriptedChatModel([Structured({"order_id": "o-9"}, tool_name="get_order")])

    reply = await model.ainvoke(QUESTION)

    assert reply.tool_calls[0]["name"] == "get_order"


async def test_an_error_step_raises_at_its_position_and_the_script_goes_on() -> None:
    model = ScriptedChatModel([Say("ok"), Fail(ConnectionError("provider down")), Say("back")])

    assert (await model.ainvoke(QUESTION)).content == "ok"
    with pytest.raises(ConnectionError, match="provider down"):
        await model.ainvoke(QUESTION)
    assert (await model.ainvoke(QUESTION)).content == "back"
    assert model.calls_made == 3


async def test_a_hang_step_never_answers_within_the_deadline() -> None:
    model = ScriptedChatModel([Hang()])

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):
            await model.ainvoke(QUESTION)

    assert model.calls_made == 1


async def test_a_hanging_call_still_propagates_cancellation() -> None:
    model = ScriptedChatModel([Hang()])
    task = asyncio.create_task(model.ainvoke(QUESTION))
    await asyncio.sleep(0)

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


def test_a_hang_step_refuses_to_block_a_synchronous_call() -> None:
    with pytest.raises(RuntimeError, match="async"):
        ScriptedChatModel([Hang()]).invoke(QUESTION)


async def test_token_counts_come_with_the_answer_including_the_reasoning_detail() -> None:
    usage = TokenUsage(input=120, output=80, reasoning=50)
    model = ScriptedChatModel([Say("answer", usage)])

    reply = await model.ainvoke(QUESTION)

    assert isinstance(reply, AIMessage)
    assert reply.usage_metadata == {
        "input_tokens": 120,
        "output_tokens": 80,
        "total_tokens": 200,
        "output_token_details": {"reasoning": 50},
    }


async def test_an_answer_without_a_count_has_no_usage() -> None:
    model = ScriptedChatModel([Say("no usage"), Say("partial", TokenUsage(input=None, output=5))])

    first = await model.ainvoke(QUESTION)
    second = await model.ainvoke(QUESTION)

    assert isinstance(first, AIMessage)
    assert isinstance(second, AIMessage)
    assert first.usage_metadata is None
    assert second.usage_metadata is None


async def test_tokens_travel_with_tool_and_structured_answers_too() -> None:
    usage = TokenUsage(input=10, output=4)
    call = ScriptedToolCall("get_order", {"order_id": "o"})
    model = ScriptedChatModel([CallTools((call,), usage=usage)])

    reply = await model.bind_tools([get_order]).ainvoke(QUESTION)

    assert isinstance(reply, AIMessage)
    assert reply.usage_metadata == {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14}


async def test_calling_past_the_end_of_the_script_cites_its_size() -> None:
    model = ScriptedChatModel([Say("only one")])
    await model.ainvoke(QUESTION)

    with pytest.raises(ScriptExhaustedError, match=r"script has 1 steps.* 2 times"):
        await model.ainvoke(QUESTION)


def test_an_empty_script_fails_clearly_on_the_first_call() -> None:
    with pytest.raises(ScriptExhaustedError, match=r"script has 0 steps.* 1 times"):
        ScriptedChatModel([]).invoke(QUESTION)


async def test_callbacks_see_the_call_as_with_any_chat_model() -> None:
    handler = RecordingHandler()
    usage = TokenUsage(input=7, output=3)
    model = ScriptedChatModel([Say("hello", usage)])

    await model.ainvoke(QUESTION, config={"callbacks": [handler]})

    assert handler.started == 1
    message = handler.ended[0].generations[0][0]
    assert isinstance(message.message, AIMessage)  # type: ignore[attr-defined]
    assert message.message.usage_metadata == {  # type: ignore[attr-defined]
        "input_tokens": 7,
        "output_tokens": 3,
        "total_tokens": 10,
    }


async def test_a_failing_step_still_reaches_the_error_callback() -> None:
    handler = RecordingHandler()
    model = ScriptedChatModel([Fail(ConnectionError("down"))])

    with pytest.raises(ConnectionError):
        await model.ainvoke(QUESTION, config={"callbacks": [handler]})

    assert handler.started == 1
    assert handler.ended == []
    assert [type(error) for error in handler.errors] == [ConnectionError]
