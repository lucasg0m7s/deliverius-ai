"""Provider integrations against simulated servers and SDK errors: nothing leaves the process."""

import json
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import anthropic
import httpx
import pytest
from google.genai import errors as genai_errors
from langchain_anthropic import ChatAnthropic
from langchain_core.exceptions import (
    ModelAPIError,
    ModelAuthenticationError,
    ModelConnectionError,
    ModelRateLimitError,
    ModelTimeoutError,
)
from langchain_core.messages import HumanMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import ChatOpenAI
from pydantic import SecretStr
from support.llm_rows import GEMINI_REF, NOW
from support.llm_runtime import FAST_RETRY, GROQ_KEY, scripted_runtime, settings_with

from deliverius.llm.call import DegradedMode, LlmReply, call_or_degrade
from deliverius.llm.factory import (
    GROQ_BASE_URL,
    GROQ_REASONING_EFFORT,
    ModelSpec,
    anthropic_model,
    default_builders,
    gemini_model,
    openai_compatible_model,
)
from deliverius.llm.gate import pause_seconds_from
from deliverius.llm.governor import UsageGovernor
from deliverius.llm.in_memory import InMemoryLlmUsageStore
from deliverius.llm.ports import BudgetLimit
from deliverius.llm.roles import ROLE_CONFIGS, ModelRole
from deliverius.llm.runtime import RETRYABLE_ERRORS, RoleModels
from deliverius.shared.clock import FixedClock
from deliverius.shared.model_ref import LlmProvider, ModelRef

QUESTION = [HumanMessage("Where is my order?")]
GROQ_REF = ModelRef(LlmProvider.GROQ, "openai/gpt-oss-120b")
Handler = Callable[[httpx.Request], httpx.Response]


def mock_client(handler: Handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def completion(
    content: str = "hi", prompt: int = 11, output: int = 7, reasoning: int | None = None
) -> httpx.Response:
    usage: dict[str, Any] = {
        "prompt_tokens": prompt,
        "completion_tokens": output,
        "total_tokens": prompt + output,
    }
    if reasoning is not None:
        usage["completion_tokens_details"] = {"reasoning_tokens": reasoning}
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-1",
            "object": "chat.completion",
            "created": 1,
            "model": "openai/gpt-oss-120b",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": usage,
        },
    )


def event_stream(content: str, prompt: int, output: int) -> httpx.Response:
    def chunk(delta: dict[str, Any], finish: str | None, usage: dict[str, int] | None) -> str:
        body: dict[str, Any] = {
            "id": "chatcmpl-1",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "openai/gpt-oss-120b",
            "choices": [] if usage else [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        if usage:
            body["usage"] = usage
        return f"data: {json.dumps(body)}\n\n"

    usage = {"prompt_tokens": prompt, "completion_tokens": output, "total_tokens": prompt + output}
    body = (
        chunk({"role": "assistant", "content": content}, None, None)
        + chunk({}, "stop", None)
        + chunk({}, None, usage)
        + "data: [DONE]\n\n"
    )
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())


def spec_for(governor: UsageGovernor | None = None) -> ModelSpec:
    return ModelSpec(
        role=ModelRole.AGENT,
        ref=GROQ_REF,
        config=ROLE_CONFIGS[ModelRole.AGENT],
        api_key=SecretStr(GROQ_KEY),
        rate_limiter=None,
        cache=None,
        callbacks=[] if governor is None else [governor],
    )


def governed(store: InMemoryLlmUsageStore, ref: ModelRef = GROQ_REF) -> UsageGovernor:
    return UsageGovernor(
        role=ModelRole.AGENT,
        model=ref,
        limit=BudgetLimit(None, None),
        store=store,
        clock=FixedClock(NOW),
    )


async def test_a_500_makes_a_single_request_per_attempt() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500, json={"error": {"message": "boom", "type": "server_error"}})

    model = openai_compatible_model(
        spec_for(), base_url=GROQ_BASE_URL, http_async_client=mock_client(handler)
    )

    with pytest.raises(ModelAPIError):
        await model.ainvoke(QUESTION)

    assert len(requests) == 1
    assert str(requests[0].url).startswith(GROQ_BASE_URL)
    assert requests[0].headers["authorization"] == f"Bearer {GROQ_KEY}"


async def test_the_reasoning_effort_reaches_the_request_body() -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return completion()

    model = openai_compatible_model(
        spec_for(),
        base_url=GROQ_BASE_URL,
        reasoning_effort=GROQ_REASONING_EFFORT,
        http_async_client=mock_client(handler),
    )

    await model.ainvoke(QUESTION)

    assert bodies[0]["reasoning_effort"] == "low"


def test_only_groq_lowers_the_reasoning_effort() -> None:
    builders = default_builders("http://localhost:11434/v1")

    groq = builders[LlmProvider.GROQ](spec_for())
    ollama = builders[LlmProvider.OLLAMA](spec_for())

    assert isinstance(groq, ChatOpenAI)
    assert isinstance(ollama, ChatOpenAI)
    assert groq.reasoning_effort == GROQ_REASONING_EFFORT
    assert ollama.reasoning_effort is None


async def test_the_retry_layer_is_the_only_one_so_three_attempts_are_three_requests() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500, json={"error": {"message": "boom", "type": "server_error"}})

    store = InMemoryLlmUsageStore()
    settings = settings_with(llm_judge_model="groq:openai/gpt-oss-120b", llm_groq_rpm=60_000)
    client = mock_client(handler)
    runtime, _, _ = scripted_runtime(
        settings,
        {
            LlmProvider.GROQ: lambda spec: openai_compatible_model(
                spec, base_url=GROQ_BASE_URL, http_async_client=client
            )
        },
        store,
    )

    result = await call_or_degrade(
        runtime.models_for(ModelRole.JUDGE).runnable(), QUESTION, ModelRole.JUDGE
    )

    assert isinstance(result, DegradedMode)
    assert len(requests) == 3
    assert len(store.calls) == 3
    usage = store.usage(LlmProvider.GROQ, NOW.date())
    assert usage is not None
    assert usage.requests_used == 3


async def test_the_usage_of_a_streamed_answer_reaches_the_row() -> None:
    store = InMemoryLlmUsageStore()
    model = openai_compatible_model(
        spec_for(governed(store)),
        base_url=GROQ_BASE_URL,
        http_async_client=mock_client(lambda _: event_stream("Your order is close.", 11, 7)),
    )

    text = "".join([chunk.text async for chunk in model.astream(QUESTION)])

    assert text == "Your order is close."
    [record] = store.calls
    assert (record.usage.input, record.usage.output) == (11, 7)
    assert record.equivalent_cost_usd_micros == round(11 * 0.15 + 7 * 0.60)


async def test_reasoning_tokens_reach_the_row_without_being_charged_twice() -> None:
    store = InMemoryLlmUsageStore()
    model = openai_compatible_model(
        spec_for(governed(store)),
        base_url=GROQ_BASE_URL,
        http_async_client=mock_client(lambda _: completion(prompt=100, output=50, reasoning=30)),
    )

    await model.ainvoke(QUESTION)

    [record] = store.calls
    assert (record.usage.input, record.usage.output, record.usage.reasoning) == (100, 50, 30)
    assert record.equivalent_cost_usd_micros == round(100 * 0.15 + 50 * 0.60)


@pytest.mark.parametrize(
    ("status", "retryable"),
    [(429, True), (500, True), (503, True), (401, False), (400, False), (404, False)],
)
async def test_compatible_server_errors_are_translated_to_the_classes_retry_looks_for(
    status: int, retryable: bool
) -> None:
    model = openai_compatible_model(
        spec_for(),
        base_url=GROQ_BASE_URL,
        http_async_client=mock_client(
            lambda _: httpx.Response(status, json={"error": {"message": "x", "type": "x"}})
        ),
    )

    with pytest.raises(Exception) as raised:  # noqa: PT011 - the class is the point of the test
        await model.ainvoke(QUESTION)

    assert isinstance(raised.value, RETRYABLE_ERRORS) is retryable


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (httpx.ReadTimeout("slow"), ModelTimeoutError),
        (httpx.ConnectError("refused"), ModelConnectionError),
    ],
    ids=["timeout", "connection"],
)
async def test_compatible_transport_errors_are_retryable(
    failure: Exception, expected: type[Exception]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise failure

    model = openai_compatible_model(
        spec_for(), base_url=GROQ_BASE_URL, http_async_client=mock_client(handler)
    )

    with pytest.raises(expected):
        await model.ainvoke(QUESTION)


async def test_a_429_with_retry_after_is_read_through_the_translated_error() -> None:
    model = openai_compatible_model(
        spec_for(),
        base_url=GROQ_BASE_URL,
        http_async_client=mock_client(
            lambda _: httpx.Response(
                429, headers={"Retry-After": "60"}, json={"error": {"message": "x", "type": "x"}}
            )
        ),
    )

    with pytest.raises(ModelRateLimitError) as raised:
        await model.ainvoke(QUESTION)

    assert pause_seconds_from(raised.value) == 60.0


def gemini_with(error: BaseException, calls: list[int] | None = None) -> ChatGoogleGenerativeAI:
    """A real Gemini model whose SDK call fails with `error`: translation is the library's own."""
    model = gemini_model(spec_for_gemini())
    assert isinstance(model, ChatGoogleGenerativeAI)

    async def fail(**kwargs: Any) -> None:
        if calls is not None:
            calls.append(1)
        raise error

    model.async_client.models.generate_content = fail  # type: ignore[method-assign]
    return model


def spec_for_gemini() -> ModelSpec:
    return ModelSpec(
        role=ModelRole.AGENT,
        ref=GEMINI_REF,
        config=ROLE_CONFIGS[ModelRole.AGENT],
        api_key=SecretStr("gemini-key"),
        rate_limiter=None,
        cache=None,
        callbacks=[],
    )


def http_response(status: int, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, request=httpx.Request("POST", "http://x"), headers=headers)


RATE_LIMIT_BODY = {
    "error": {
        "code": 429,
        "status": "RESOURCE_EXHAUSTED",
        "details": [
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "37s"},
        ],
    }
}


async def test_gemini_429_is_retryable_and_carries_the_retry_delay() -> None:
    error = genai_errors.ClientError(429, RATE_LIMIT_BODY, http_response(429))

    with pytest.raises(ModelRateLimitError) as raised:
        await gemini_with(error).ainvoke(QUESTION)

    assert isinstance(raised.value, RETRYABLE_ERRORS)
    assert pause_seconds_from(raised.value) == 37.0


async def test_gemini_429_with_a_retry_after_header_uses_the_header() -> None:
    error = genai_errors.ClientError(
        429, RATE_LIMIT_BODY, http_response(429, {"Retry-After": "12"})
    )

    with pytest.raises(ModelRateLimitError) as raised:
        await gemini_with(error).ainvoke(QUESTION)

    assert pause_seconds_from(raised.value) == 12.0


async def test_gemini_5xx_is_retryable() -> None:
    error = genai_errors.ServerError(503, {"error": {"message": "unavailable"}}, http_response(503))

    with pytest.raises(ModelAPIError):
        await gemini_with(error).ainvoke(QUESTION)


async def test_gemini_401_is_not_retryable() -> None:
    error = genai_errors.ClientError(401, {"error": {"message": "key"}}, http_response(401))

    with pytest.raises(ModelAuthenticationError) as raised:
        await gemini_with(error).ainvoke(QUESTION)

    assert not isinstance(raised.value, RETRYABLE_ERRORS)


@pytest.mark.parametrize(
    ("failure", "attempts"),
    [
        (httpx.ReadTimeout("slow"), 3),
        (httpx.ConnectTimeout("slow"), 3),
        (httpx.ConnectError("refused"), 3),
        (httpx.ReadError("reset"), 3),
        (httpx.UnsupportedProtocol("no scheme"), 1),
        (httpx.ProxyError("bad proxy"), 1),
    ],
    ids=["read-timeout", "connect-timeout", "connection", "read-error", "protocol", "proxy"],
)
async def test_gemini_transport_errors_are_retried_only_when_repeating_can_help(
    failure: Exception, attempts: int
) -> None:
    """`langchain_google_genai` lets raw httpx errors through, so the retry list names them.

    Timeouts and network failures repeat; configuration errors (unsupported protocol, proxy) do
    not, and go straight to the fallback.
    """
    calls: list[int] = []
    models = RoleModels(
        role=ModelRole.JUDGE,
        primary=gemini_with(failure, calls),
        primary_ref=GEMINI_REF,
        fallback=None,
        fallback_ref=None,
        retry=FAST_RETRY,
    )

    with pytest.raises(httpx.HTTPError):
        await models.runnable().ainvoke(QUESTION)

    assert len(calls) == attempts


def anthropic_with(error: BaseException) -> ChatAnthropic:
    spec = ModelSpec(
        role=ModelRole.AGENT,
        ref=ModelRef(LlmProvider.ANTHROPIC, "claude-x"),
        config=ROLE_CONFIGS[ModelRole.AGENT],
        api_key=SecretStr("anthropic-key"),
        rate_limiter=None,
        cache=None,
        callbacks=[],
    )
    model = anthropic_model(spec)
    assert isinstance(model, ChatAnthropic)

    class RawResponse:
        async def create(self, **kwargs: Any) -> None:
            raise error

    class Messages:
        with_raw_response = RawResponse()

    class Client:
        messages = Messages()

    model.__dict__["_async_client"] = Client()
    return model


ANTHROPIC_REQUEST = httpx.Request("POST", "http://x")


@pytest.mark.parametrize(
    ("error", "retryable"),
    [
        (anthropic.RateLimitError("limit", response=http_response(429), body=None), True),
        (anthropic.InternalServerError("server", response=http_response(500), body=None), True),
        (anthropic.OverloadedError("busy", response=http_response(529), body=None), True),
        (anthropic.APITimeoutError(request=ANTHROPIC_REQUEST), True),
        (anthropic.APIConnectionError(request=ANTHROPIC_REQUEST), True),
        (anthropic.AuthenticationError("key", response=http_response(401), body=None), False),
        (anthropic.BadRequestError("bad", response=http_response(400), body=None), False),
    ],
    ids=["429", "500", "529", "timeout", "connection", "401", "400"],
)
async def test_anthropic_errors_are_translated_to_the_classes_retry_looks_for(
    error: Exception, retryable: bool
) -> None:
    with pytest.raises(Exception) as raised:  # noqa: PT011 - the class is the point of the test
        await anthropic_with(error).ainvoke(QUESTION)

    assert isinstance(raised.value, RETRYABLE_ERRORS) is retryable


async def test_anthropic_429_carries_the_retry_after_header() -> None:
    error = anthropic.RateLimitError(
        "limit", response=http_response(429, {"retry-after": "30"}), body=None
    )

    with pytest.raises(ModelRateLimitError) as raised:
        await anthropic_with(error).ainvoke(QUESTION)

    assert pause_seconds_from(raised.value) == 30.0


async def test_a_429_with_retry_after_goes_to_the_reserve_and_quiets_the_primary() -> None:
    primary_requests: list[httpx.Request] = []
    reserve_requests: list[httpx.Request] = []

    def primary(request: httpx.Request) -> httpx.Response:
        primary_requests.append(request)
        return httpx.Response(
            429, headers={"Retry-After": "60"}, json={"error": {"message": "x", "type": "x"}}
        )

    def reserve(request: httpx.Request) -> httpx.Response:
        reserve_requests.append(request)
        return completion("from the reserve")

    primary_client, reserve_client = mock_client(primary), mock_client(reserve)
    settings = settings_with(
        llm_agent_model="groq:openai/gpt-oss-120b",
        llm_agent_fallback="ollama:llama3.2:3b",
        llm_groq_rpm=60_000,
    )
    runtime, store, clock = scripted_runtime(
        settings,
        {
            LlmProvider.GROQ: lambda spec: openai_compatible_model(
                spec, base_url=GROQ_BASE_URL, http_async_client=primary_client
            ),
            LlmProvider.OLLAMA: lambda spec: openai_compatible_model(
                spec, base_url="http://localhost:11434/v1", http_async_client=reserve_client
            ),
        },
    )
    runnable = runtime.models_for(ModelRole.AGENT).runnable()

    first = await call_or_degrade(runnable, QUESTION, ModelRole.AGENT)
    second = await call_or_degrade(runnable, QUESTION, ModelRole.AGENT)

    assert isinstance(first, LlmReply)
    assert first.message.content == "from the reserve"
    assert isinstance(second, LlmReply)
    assert len(primary_requests) == 1
    assert len(reserve_requests) == 2

    clock.advance(timedelta(seconds=57))
    await call_or_degrade(runnable, QUESTION, ModelRole.AGENT)
    assert len(primary_requests) == 1

    clock.advance(timedelta(seconds=3))
    await call_or_degrade(runnable, QUESTION, ModelRole.AGENT)
    assert len(primary_requests) == 2
    first_row = store.calls[0]
    assert (first_row.provider, first_row.status.value) == (LlmProvider.GROQ, "error")
