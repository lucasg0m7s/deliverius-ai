from collections.abc import Iterable
from dataclasses import dataclass
from typing import Self

from deliverius.shared.errors import InvariantError


@dataclass(frozen=True, slots=True, order=True)
class Money:
    """A non-negative amount in BRL, stored as integer cents so arithmetic is exact."""

    cents: int

    def __post_init__(self) -> None:
        # bool is a subclass of int, so `type(...) is int` is what rejects True/False.
        if type(self.cents) is not int:
            raise InvariantError("money.cents_not_int")
        if self.cents < 0:
            raise InvariantError("money.negative")

    @classmethod
    def zero(cls) -> Self:
        return cls(0)

    @classmethod
    def sum(cls, amounts: Iterable[Self]) -> Self:
        total = cls.zero()
        for amount in amounts:
            total = total + amount
        return total

    def __add__(self, other: Self) -> Self:
        return type(self)(self.cents + other.cents)

    def __sub__(self, other: Self) -> Self:
        if other.cents > self.cents:
            raise InvariantError("money.negative")
        return type(self)(self.cents - other.cents)

    def __mul__(self, factor: int) -> Self:
        if type(factor) is not int:
            raise InvariantError("money.factor_not_int")
        return type(self)(self.cents * factor)

    def fraction(self, numerator: int, denominator: int) -> Self:
        """`self * numerator / denominator`, rounded half up to the cent using integers only."""
        if numerator < 0 or denominator <= 0:
            raise InvariantError("money.invalid_fraction")
        return type(self)((self.cents * numerator * 2 + denominator) // (2 * denominator))

    def format_brl(self) -> str:
        """Display only: `R$ 1.234,56`. Never parse this back."""
        reais, cents = divmod(self.cents, 100)
        return f"R$ {reais:,}".replace(",", ".") + f",{cents:02d}"
