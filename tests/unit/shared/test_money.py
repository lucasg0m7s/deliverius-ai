from fractions import Fraction

import pytest
from hypothesis import given
from hypothesis import strategies as st

from deliverius.shared.errors import InvariantError
from deliverius.shared.money import Money

cents = st.integers(min_value=0, max_value=10**12)
money = cents.map(Money)


@given(money, money, money)
def test_addition_is_associative(a: Money, b: Money, c: Money) -> None:
    assert (a + b) + c == a + (b + c)


@given(st.lists(cents, max_size=50))
def test_sum_loses_nothing(values: list[int]) -> None:
    assert Money.sum(Money(value) for value in values).cents == sum(values)


@given(money, money)
def test_subtraction_is_never_negative(a: Money, b: Money) -> None:
    if b > a:
        with pytest.raises(InvariantError, match=r"money\.negative"):
            _ = a - b
    else:
        assert (a - b).cents == a.cents - b.cents


@given(money, st.integers(min_value=0, max_value=1_000), st.integers(min_value=1, max_value=1_000))
def test_fraction_is_within_half_a_cent_of_the_exact_value(
    amount: Money, numerator: int, denominator: int
) -> None:
    exact = Fraction(amount.cents * numerator, denominator)

    result = amount.fraction(numerator, denominator)

    assert abs(result.cents - exact) <= Fraction(1, 2)


def test_half_of_five_cents_rounds_half_up_to_three() -> None:
    assert Money(5).fraction(50, 100) == Money(3)


def test_fraction_rounds_below_half_down() -> None:
    assert Money(10).fraction(1, 3) == Money(3)


@pytest.mark.parametrize("bad", [True, 1.5, "10"])
def test_non_int_cents_are_rejected(bad: object) -> None:
    with pytest.raises(InvariantError, match=r"money\.cents_not_int"):
        Money(bad)  # type: ignore[arg-type]


def test_negative_amount_is_rejected() -> None:
    with pytest.raises(InvariantError, match=r"money\.negative"):
        Money(-1)


def test_multiplication_by_int() -> None:
    assert Money(250) * 3 == Money(750)


@pytest.mark.parametrize("bad", [True, 1.5])
def test_multiplication_by_non_int_is_rejected(bad: object) -> None:
    with pytest.raises(InvariantError, match=r"money\.factor_not_int"):
        _ = Money(250) * bad  # type: ignore[operator]


@pytest.mark.parametrize(("numerator", "denominator"), [(-1, 2), (1, 0), (1, -2)])
def test_invalid_fraction_is_rejected(numerator: int, denominator: int) -> None:
    with pytest.raises(InvariantError, match=r"money\.invalid_fraction"):
        Money(100).fraction(numerator, denominator)


def test_zero_and_ordering() -> None:
    assert Money.zero() == Money(0)
    assert Money(1) > Money.zero()


@pytest.mark.parametrize(
    ("amount", "expected"),
    [(0, "R$ 0,00"), (5, "R$ 0,05"), (123456, "R$ 1.234,56"), (100000000, "R$ 1.000.000,00")],
)
def test_format_brl(amount: int, expected: str) -> None:
    assert Money(amount).format_brl() == expected
