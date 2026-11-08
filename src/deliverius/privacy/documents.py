"""Check digits of Brazilian documents and payment cards. Input holds only the value itself."""

from collections.abc import Sequence
from itertools import cycle

_CPF_LENGTH = 11
_CNPJ_LENGTH = 14
_CNPJ_WEIGHTS = (2, 3, 4, 5, 6, 7, 8, 9)
_ZERO = ord("0")


def is_valid_cpf(digits: str) -> bool:
    if len(digits) != _CPF_LENGTH or not digits.isdecimal():
        return False
    values = [int(digit) for digit in digits]
    first = _check_digit(values[:9], range(10, 1, -1))
    second = _check_digit(values[:10], range(11, 1, -1))
    return values[9:] == [first, second]


def is_valid_cnpj(characters: str) -> bool:
    """Numeric or alphanumeric: each character counts as its ASCII code minus 48 (Receita)."""
    if len(characters) != _CNPJ_LENGTH or not characters.isascii() or not characters.isalnum():
        return False
    values = [ord(character) - _ZERO for character in characters.upper()]
    first = _check_digit(values[:12], _cnpj_weights(12))
    second = _check_digit(values[:13], _cnpj_weights(13))
    return values[12:] == [first, second]


def passes_luhn(digits: str) -> bool:
    if not digits.isdecimal():
        return False
    total = 0
    for position, digit in enumerate(reversed(digits)):
        value = int(digit)
        if position % 2:
            value *= 2
        total += value - 9 if value > 9 else value
    return total % 10 == 0


def _cnpj_weights(length: int) -> list[int]:
    """Weights run from 2 to 9 starting at the rightmost character, then restart."""
    weights = cycle(_CNPJ_WEIGHTS)
    return [next(weights) for _ in range(length)][::-1]


def _check_digit(values: Sequence[int], weights: Sequence[int]) -> int:
    remainder = sum(value * weight for value, weight in zip(values, weights, strict=True)) % 11
    return 0 if remainder < 2 else 11 - remainder
