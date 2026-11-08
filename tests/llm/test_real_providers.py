"""Real calls to Gemini and Groq: one with a tool and one with structured output each.

Run only on request (`uv run pytest -m llm --no-cov`): they cost quota. Without the provider key a
test FAILS naming the variable instead of skipping, so a green run always means a real call.
The usage rows go to the in-memory store: Postgres writes are covered by the integration tests.
"""

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from pydantic import BaseModel

from deliverius.config import Settings
from deliverius.llm.in_memory import InMemoryLlmUsageStore
from deliverius.llm.ports import CallStatus
from deliverius.llm.roles import ModelRole
from deliverius.llm.runtime import API_KEY_VARIABLES, LlmRuntime
from deliverius.shared.clock import SystemClock
from deliverius.shared.model_ref import LlmProvider, ModelRef

# One event loop for the module: langchain_openai caches one async HTTP client per process, and it
# breaks when a later test runs on a new loop ("Event loop is closed").
pytestmark = [pytest.mark.llm, pytest.mark.asyncio(loop_scope="module")]

GEMINI_MODEL = ModelRef(LlmProvider.GEMINI, "gemini-3.5-flash-lite")
GROQ_MODEL = ModelRef(LlmProvider.GROQ, "openai/gpt-oss-120b")
CASES = [
    pytest.param(GEMINI_MODEL, ModelRole.AGENT, id="gemini"),
    pytest.param(GROQ_MODEL, ModelRole.JUDGE, id="groq"),
]


class OrderLookup(BaseModel):
    """Which order the customer is asking about."""

    order_number: str


@tool
def get_order_status(order_number: str) -> str:
    """Return the delivery status of an order by its number."""
    return f"order {order_number} is out for delivery"


def build(model: ModelRef, role: ModelRole) -> tuple[LlmRuntime, InMemoryLlmUsageStore]:
    variable = API_KEY_VARIABLES[model.provider]
    settings = Settings(llm_agent_model=model, llm_judge_model=model)  # type: ignore[call-arg]
    key = settings.gemini_api_key if model.provider is LlmProvider.GEMINI else settings.groq_api_key
    if key is None:
        pytest.fail(f"{variable} is not set (environment or .env): {model} cannot be called")
    store = InMemoryLlmUsageStore()
    return LlmRuntime(settings, store, SystemClock(), roles=[role]), store


def assert_billed_row(store: InMemoryLlmUsageStore, model: ModelRef) -> None:
    [row] = store.calls
    assert row.status is CallStatus.OK
    assert (row.provider, row.model) == (model.provider, model.model)
    assert row.usage.input is not None
    assert row.usage.input > 0
    assert row.usage.output is not None
    assert row.usage.output > 0
    assert row.equivalent_cost_usd_micros is not None
    assert row.latency_ms > 0


@pytest.mark.parametrize(("model", "role"), CASES)
async def test_a_call_with_a_tool_returns_the_tool_call_and_a_billed_row(
    model: ModelRef, role: ModelRole
) -> None:
    runtime, store = build(model, role)
    chat_model = runtime.models_for(role).primary

    reply = await chat_model.bind_tools([get_order_status]).ainvoke(
        [HumanMessage("Where is order 4821? Use the tool to check its status.")]
    )

    assert isinstance(reply, AIMessage)
    assert [call["name"] for call in reply.tool_calls] == ["get_order_status"]
    assert_billed_row(store, model)


@pytest.mark.parametrize(("model", "role"), CASES)
async def test_a_call_with_structured_output_returns_the_validated_object_and_a_billed_row(
    model: ModelRef, role: ModelRole
) -> None:
    runtime, store = build(model, role)
    chat_model = runtime.models_for(role).primary

    answer = await chat_model.with_structured_output(OrderLookup).ainvoke(
        [HumanMessage("My order number is 4821 and it is late.")]
    )

    assert isinstance(answer, OrderLookup)
    assert "4821" in answer.order_number
    assert_billed_row(store, model)
