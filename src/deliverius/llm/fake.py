"""A chat model that follows a script, for tests: no network, and the same contract as a real one.

Each call to the model consumes one step, in order. Tools and structured output work as with a
real provider (`bind_tools`, `with_structured_output`), and every bound copy shares the one
script, so the position survives the binding. Callbacks run around it as for any chat model.
"""

import asyncio
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, NoReturn, override

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel, LanguageModelInput
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.messages.ai import UsageMetadata
from langchain_core.messages.tool import ToolCall
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import BaseModel, PrivateAttr

from deliverius.llm.pricing import TokenUsage


class ScriptExhaustedError(RuntimeError):
    """The model was called more times than its script has steps."""

    def __init__(self, steps: int, calls: int) -> None:
        super().__init__(f"the script has {steps} steps and the model was called {calls} times")


class HangNeedsAsyncError(RuntimeError):
    """A `Hang` step would block a synchronous call forever."""

    def __init__(self) -> None:
        super().__init__("a Hang step needs the async API: a sync call would block forever")


class StructuredSchemaError(ValueError):
    """A `Structured` step has no schema to answer: none was bound and no tool name was given."""

    def __init__(self) -> None:
        super().__init__("a Structured step needs exactly one bound schema, or a tool_name")


@dataclass(frozen=True, slots=True)
class Say:
    """Answer with plain text."""

    text: str
    usage: TokenUsage | None = None


@dataclass(frozen=True, slots=True)
class ScriptedToolCall:
    name: str
    args: Mapping[str, Any]
    id: str | None = None


@dataclass(frozen=True, slots=True)
class CallTools:
    """Answer asking for one or more tools, optionally with some text."""

    calls: tuple[ScriptedToolCall, ...]
    text: str = ""
    usage: TokenUsage | None = None


@dataclass(frozen=True, slots=True)
class Structured:
    """Answer the schema bound by `with_structured_output`, as a real provider would.

    Without `tool_name`, the name is taken from the single tool bound to the model.
    """

    value: BaseModel | Mapping[str, Any]
    tool_name: str | None = None
    usage: TokenUsage | None = None


@dataclass(frozen=True, slots=True)
class Fail:
    """Raise this error instead of answering."""

    error: Exception


@dataclass(frozen=True, slots=True)
class Hang:
    """Never answer: only the caller's deadline or a cancellation ends the call. Async only."""


type Step = Say | CallTools | Structured | Fail | Hang

type BindableTool = dict[str, Any] | type | Callable[..., Any] | BaseTool


class ScriptedChatModel(BaseChatModel):
    _script: tuple[Step, ...] = PrivateAttr()
    _position: int = PrivateAttr(default=0)
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def __init__(self, script: Sequence[Step], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._script = tuple(script)

    @property
    def calls_made(self) -> int:
        """How many times the model was called, counting calls that failed or hung."""
        return self._position

    @property
    @override
    def _llm_type(self) -> str:
        return "scripted"

    @override
    def bind_tools(
        self,
        tools: Sequence[BindableTool],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable[LanguageModelInput, AIMessage]:
        formatted = [convert_to_openai_tool(tool) for tool in tools]
        return self.bind(tools=formatted, tool_choice=tool_choice, **kwargs)

    @override
    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        index, step = self._take_step()
        if isinstance(step, Hang):
            raise HangNeedsAsyncError
        return _result_for(step, step_index=index, bound_tools=kwargs.get("tools"))

    @override
    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        index, step = self._take_step()
        if isinstance(step, Hang):
            await _block_forever()
        return _result_for(step, step_index=index, bound_tools=kwargs.get("tools"))

    def _take_step(self) -> tuple[int, Step]:
        with self._lock:
            if self._position >= len(self._script):
                raise ScriptExhaustedError(len(self._script), self._position + 1)
            index = self._position
            self._position += 1
        return index, self._script[index]


async def _block_forever() -> NoReturn:
    await asyncio.Event().wait()
    raise AssertionError


def _result_for(
    step: Say | CallTools | Structured | Fail, *, step_index: int, bound_tools: object
) -> ChatResult:
    match step:
        case Say(text=text, usage=usage):
            message = AIMessage(content=text, usage_metadata=_usage_metadata(usage))
        case CallTools(calls=calls, text=text, usage=usage):
            tool_calls = [_tool_call(call, step_index, i) for i, call in enumerate(calls)]
            message = AIMessage(
                content=text, tool_calls=tool_calls, usage_metadata=_usage_metadata(usage)
            )
        case Structured(value=value, tool_name=tool_name, usage=usage):
            call = ScriptedToolCall(
                name=tool_name or _single_bound_tool_name(bound_tools),
                args=value.model_dump(mode="json") if isinstance(value, BaseModel) else value,
            )
            message = AIMessage(
                content="",
                tool_calls=[_tool_call(call, step_index, 0)],
                usage_metadata=_usage_metadata(usage),
            )
        case Fail(error=error):
            raise error
    return ChatResult(generations=[ChatGeneration(message=message)])


def _tool_call(call: ScriptedToolCall, step_index: int, call_index: int) -> ToolCall:
    return ToolCall(
        name=call.name,
        args=dict(call.args),
        id=call.id or f"call-{step_index}-{call_index}",
    )


def _single_bound_tool_name(bound_tools: object) -> str:
    if not isinstance(bound_tools, list) or len(bound_tools) != 1:
        raise StructuredSchemaError
    name: str = bound_tools[0]["function"]["name"]
    return name


def _usage_metadata(usage: TokenUsage | None) -> UsageMetadata | None:
    if usage is None or usage.input is None or usage.output is None:
        return None
    metadata = UsageMetadata(
        input_tokens=usage.input,
        output_tokens=usage.output,
        total_tokens=usage.input + usage.output,
    )
    if usage.reasoning is not None:
        metadata["output_token_details"] = {"reasoning": usage.reasoning}
    return metadata
