"""The result of `call_or_degrade` is closed: mypy proves this `match` covers every case."""

from typing import assert_never

from langchain_core.messages import AIMessage

from deliverius.llm.call import DegradedMode, DegradedReason, LlmReply


def describe(result: LlmReply | DegradedMode) -> str:
    match result:
        case LlmReply(message=message):
            return f"reply:{message.content}"
        case DegradedMode(reason=reason):
            return f"degraded:{reason}"
        case _:
            assert_never(result)


def describe_reason(reason: DegradedReason) -> str:
    match reason:
        case DegradedReason.DEADLINE:
            return "too slow"
        case DegradedReason.BUDGET_EXHAUSTED | DegradedReason.BUDGET_UNAVAILABLE:
            return "no budget"
        case DegradedReason.PROVIDER_PAUSED | DegradedReason.PROVIDER_ERROR:
            return "provider"
        case DegradedReason.INVALID_OUTPUT:
            return "invalid output"
        case _:
            assert_never(reason)


def test_both_results_are_told_apart() -> None:
    assert describe(LlmReply(AIMessage(content="hi"))) == "reply:hi"
    assert describe(DegradedMode(DegradedReason.DEADLINE)) == "degraded:deadline"


def test_every_degraded_reason_has_a_branch() -> None:
    assert {describe_reason(reason) for reason in DegradedReason} == {
        "too slow",
        "no budget",
        "provider",
        "invalid output",
    }
