from decimal import Decimal

import pytest

from deliverius.llm.pricing import PRICES, ModelPrice, TokenUsage, equivalent_cost_usd_micros
from deliverius.shared.errors import InvariantError
from deliverius.shared.model_ref import LlmProvider, ModelRef

GEMINI = ModelRef(LlmProvider.GEMINI, "gemini-3.5-flash-lite")
GPT_OSS_120B = ModelRef(LlmProvider.GROQ, "openai/gpt-oss-120b")
GPT_OSS_20B = ModelRef(LlmProvider.GROQ, "openai/gpt-oss-20b")


def test_the_three_listed_models_have_the_list_prices() -> None:
    assert PRICES[GEMINI] == ModelPrice(Decimal("0.30"), Decimal("2.50"))
    assert PRICES[GPT_OSS_120B] == ModelPrice(Decimal("0.15"), Decimal("0.60"))
    assert PRICES[GPT_OSS_20B] == ModelPrice(Decimal("0.075"), Decimal("0.30"))


def test_a_million_tokens_cost_the_list_price_in_micro_dollars() -> None:
    usage = TokenUsage(input=1_000_000, output=1_000_000)

    assert equivalent_cost_usd_micros(GEMINI, usage) == 2_800_000


def test_cost_adds_input_and_output_at_their_own_price() -> None:
    usage = TokenUsage(input=2_000, output=500)

    assert equivalent_cost_usd_micros(GEMINI, usage) == 600 + 1_250


@pytest.mark.parametrize(
    ("model", "input_tokens", "expected"),
    [
        (GEMINI, 1, 0),  # 0.3 micro-dollars rounds down
        (GEMINI, 2, 1),  # 0.6 rounds up
        (GPT_OSS_20B, 20, 2),  # exactly 1.5 rounds half up
        (GPT_OSS_20B, 60, 5),  # exactly 4.5 rounds half up, not to even
        (GPT_OSS_20B, 6, 0),  # 0.45 rounds down
    ],
)
def test_the_total_is_rounded_once_half_up(
    model: ModelRef, input_tokens: int, expected: int
) -> None:
    usage = TokenUsage(input=input_tokens, output=0)

    assert equivalent_cost_usd_micros(model, usage) == expected


def test_rounding_applies_to_the_sum_not_to_each_part() -> None:
    # 1 input token (0.30) plus 1 output token (2.50) is 2.8 micro-dollars: 3, not 0 + 3 from parts.
    usage = TokenUsage(input=1, output=1)

    assert equivalent_cost_usd_micros(GEMINI, usage) == 3


def test_zero_tokens_cost_zero() -> None:
    assert equivalent_cost_usd_micros(GEMINI, TokenUsage(input=0, output=0)) == 0


def test_a_model_without_a_price_has_no_cost_instead_of_zero() -> None:
    unknown = ModelRef(LlmProvider.OLLAMA, "llama3.2:3b")

    assert equivalent_cost_usd_micros(unknown, TokenUsage(input=100, output=100)) is None


@pytest.mark.parametrize(
    "usage",
    [
        TokenUsage(input=None, output=10),
        TokenUsage(input=10, output=None),
        TokenUsage(input=None, output=None),
    ],
    ids=["missing-input", "missing-output", "missing-both"],
)
def test_a_missing_token_count_leaves_the_cost_empty(usage: TokenUsage) -> None:
    assert equivalent_cost_usd_micros(GEMINI, usage) is None


def test_reasoning_tokens_are_not_charged_twice() -> None:
    without_breakdown = TokenUsage(input=1_000, output=800)
    with_breakdown = TokenUsage(input=1_000, output=800, reasoning=600)

    assert equivalent_cost_usd_micros(GEMINI, with_breakdown) == equivalent_cost_usd_micros(
        GEMINI, without_breakdown
    )
    assert equivalent_cost_usd_micros(GEMINI, with_breakdown) == 300 + 2_000


@pytest.mark.parametrize("field", ["input", "output", "reasoning"])
@pytest.mark.parametrize("value", [-1, True, 1.5])
def test_an_invalid_token_count_is_a_bug(field: str, value: object) -> None:
    counts: dict[str, object] = {"input": 1, "output": 1, "reasoning": 1} | {field: value}

    with pytest.raises(InvariantError) as error:
        TokenUsage(**counts)  # type: ignore[arg-type]

    assert error.value.rule == "token_usage.invalid"
