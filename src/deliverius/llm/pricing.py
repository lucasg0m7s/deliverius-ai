"""List prices and the equivalent cost of a call: what it would cost without the free quota."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from types import MappingProxyType

from deliverius.shared.errors import InvariantError
from deliverius.shared.model_ref import LlmProvider, ModelRef

PRICES_CHECKED_ON = date(2026, 11, 2)


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """US dollars per million tokens, which is also micro-dollars per token."""

    input: Decimal
    output: Decimal


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Tokens as the provider reports them; `None` means the provider did not say.

    `output` already contains the reasoning tokens, so `reasoning` is a breakdown, not an extra.
    """

    input: int | None
    output: int | None
    reasoning: int | None = None

    def __post_init__(self) -> None:
        for count in (self.input, self.output, self.reasoning):
            if count is not None and (type(count) is not int or count < 0):
                raise InvariantError("token_usage.invalid")


PRICES: Mapping[ModelRef, ModelPrice] = MappingProxyType(
    {
        ModelRef(LlmProvider.GEMINI, "gemini-3.5-flash-lite"): ModelPrice(
            Decimal("0.30"), Decimal("2.50")
        ),
        ModelRef(LlmProvider.GROQ, "openai/gpt-oss-120b"): ModelPrice(
            Decimal("0.15"), Decimal("0.60")
        ),
        ModelRef(LlmProvider.GROQ, "openai/gpt-oss-20b"): ModelPrice(
            Decimal("0.075"), Decimal("0.30")
        ),
    }
)


def equivalent_cost_usd_micros(model: ModelRef, usage: TokenUsage) -> int | None:
    """List-price cost in micro-dollars, or `None` when the price or a token count is unknown.

    `None` is not zero: an unknown cost must not make a sum look cheaper than it was. The total is
    rounded once, half up, so many small calls do not each lose a fraction.
    """
    price = PRICES.get(model)
    if price is None or usage.input is None or usage.output is None:
        return None
    micros = usage.input * price.input + usage.output * price.output
    return int(micros.quantize(Decimal(1), rounding=ROUND_HALF_UP))
