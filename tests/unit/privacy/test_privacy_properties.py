"""Properties that hold for any text (Hypothesis)."""

from hypothesis import example, given
from hypothesis import strategies as st

from deliverius.privacy import (
    MIN_KEY_BYTES,
    MarkerLedger,
    mask_text,
    normalize_text,
    redact_text,
)

KEY = b"k" * MIN_KEY_BYTES
# Mostly the characters the patterns care about, so generated text actually gets close to a value.
PATTERN_ALPHABET = "0123456789 .-/@+()\n,AaBb" + "ruaav\u00ba\u200b\u2013\u0661\uff11"
pattern_like_text = st.text(alphabet=PATTERN_ALPHABET, max_size=120)
any_text = st.one_of(st.text(max_size=200), pattern_like_text)


def cpf_from(base: list[int]) -> str:
    first = _check_digit(base, range(10, 1, -1))
    second = _check_digit([*base, first], range(11, 1, -1))
    return "".join(str(digit) for digit in [*base, first, second])


def _check_digit(values: list[int], weights: range) -> int:
    remainder = sum(value * weight for value, weight in zip(values, weights, strict=True)) % 11
    return 0 if remainder < 2 else 11 - remainder


cpfs = st.lists(st.integers(0, 9), min_size=9, max_size=9).map(cpf_from)
surrounding_text = st.text(alphabet="abc ,.!?\n", max_size=40)


@given(any_text)
@example("a\u200b\u0301")
@example("\ufdfa")
def test_normalizing_twice_changes_nothing(text: str) -> None:
    once = normalize_text(text)

    assert normalize_text(once) == once


@given(any_text)
def test_redacting_twice_changes_nothing(text: str) -> None:
    once = redact_text(text)

    assert redact_text(once) == once


@given(any_text)
def test_masking_twice_changes_nothing(text: str) -> None:
    once = mask_text(text, MarkerLedger.empty("c"), KEY)

    assert mask_text(once.text, once.ledger, KEY).text == once.text


@given(cpfs, surrounding_text, surrounding_text)
def test_a_valid_cpf_never_survives(cpf: str, before: str, after: str) -> None:
    text = f"{before} {cpf} {after}"

    assert cpf not in redact_text(text)
    assert cpf not in mask_text(text, MarkerLedger.empty("c"), KEY).text
