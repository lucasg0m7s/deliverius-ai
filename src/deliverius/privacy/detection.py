"""Finds personal data in normalized text.

Every repetition is bounded, because `re` has no timeout and this runs on text from outside (ReDoS).
Patterns use `[0-9]`, never `\\d`, which also matches any Unicode digit.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from deliverius.privacy.documents import is_valid_cnpj, is_valid_cpf, passes_luhn

_MIN_CARD_DIGITS = 12
_MAX_CARD_DIGITS = 19
# Stands in for a recognized value, so no later pattern can match across it.
_BLANK = "\x00"
_NOT_AFTER_ALNUM = r"(?<![0-9A-Za-z])"
_NOT_BEFORE_ALNUM = r"(?![0-9A-Za-z])"


class PersonalDataKind(StrEnum):
    """The value is the label inside a marker, which the LLM reads: it stays in Portuguese."""

    CPF = "CPF"
    CNPJ = "CNPJ"
    CARD = "CARTAO"
    PHONE = "TELEFONE"
    EMAIL = "EMAIL"
    POSTAL_CODE = "CEP"
    ADDRESS = "ENDERECO"


@dataclass(frozen=True, slots=True)
class PersonalDataMatch:
    kind: PersonalDataKind
    start: int
    end: int


_CPF_PUNCTUATED = re.compile(r"[0-9]{3}\.[0-9]{3}\.[0-9]{3}-[0-9]{2}")
_CNPJ_PUNCTUATED = re.compile(
    r"[0-9A-Za-z]{2}\.[0-9A-Za-z]{3}\.[0-9A-Za-z]{3}/[0-9A-Za-z]{4}-[0-9]{2}"
)
_NON_ALNUM = re.compile(r"[^0-9A-Za-z]")


def _alnum_only(value: str) -> str:
    return _NON_ALNUM.sub("", value)


def _is_cpf(value: str) -> bool:
    """Full punctuation rarely happens by chance, and a mistyped document still identifies one."""
    return bool(_CPF_PUNCTUATED.fullmatch(value)) or is_valid_cpf(_alnum_only(value))


def _is_cnpj(value: str) -> bool:
    return bool(_CNPJ_PUNCTUATED.fullmatch(value)) or is_valid_cnpj(_alnum_only(value))


def _is_card(value: str) -> bool:
    digits = _alnum_only(value)
    return _MIN_CARD_DIGITS <= len(digits) <= _MAX_CARD_DIGITS and passes_luhn(digits)


def _any_match(_value: str) -> bool:
    return True


@dataclass(frozen=True, slots=True)
class _Detector:
    """`kind=None` protects the match: it is left as is, and no later pattern can touch it."""

    kind: PersonalDataKind | None
    pattern: re.Pattern[str]
    accepts: Callable[[str], bool] = _any_match


def _bounded(pattern: str) -> re.Pattern[str]:
    return re.compile(f"{_NOT_AFTER_ALNUM}{pattern}{_NOT_BEFORE_ALNUM}")


# Short on purpose: `via`, `vale`, `conjunto` and `parque` turned ordinary sentences into addresses.
_STREET_TYPES = (
    r"rua|r\.|avenida|av\.?|travessa|trav\.?|alameda|al\.|estrada|rodovia|rod\.|pra[çc]a|largo"
    r"|ladeira|viela|vila|quadra|condom[ií]nio|residencial|loteamento|jardim"
)
_STREET_WORD = r"[^\W\d_.'-][\w.'-]{0,29}"
# NFKC turns `nº` into `no`. The date prefix covers streets such as `Rua 7 de Setembro, 100`.
_ADDRESS = (
    rf"(?<![^\W\d_])(?:{_STREET_TYPES})\s+(?:[0-9]{{1,2}}\s+de\s+)?(?:{_STREET_WORD}\s+){{0,7}}?"
    rf"(?:{_STREET_WORD})?,?\s*(?:n[o°.]{{0,2}}\s*)?[0-9]{{1,5}}{_NOT_BEFORE_ALNUM}"
)

# The order decides who wins. An email comes first so `11987654321@x.com` is not read as a phone,
# and a card accepts only contiguous digits or clean groups so it does not swallow `+55 11 9...`.
_DETECTORS: tuple[_Detector, ...] = (
    _Detector(
        None,
        re.compile(
            r"(?<![0-9a-fA-F-])[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
            r"-[0-9a-fA-F]{12}(?![0-9a-fA-F-])"
        ),
    ),
    _Detector(
        PersonalDataKind.EMAIL,
        re.compile(r"[\w.+%-]{1,64}@[\w-]{1,63}(?:\.[\w-]{1,63}){1,8}"),
    ),
    _Detector(
        PersonalDataKind.CNPJ,
        _bounded(r"[0-9A-Za-z]{2}\.?[0-9A-Za-z]{3}\.?[0-9A-Za-z]{3}/?[0-9A-Za-z]{4}-?[0-9]{2}"),
        _is_cnpj,
    ),
    _Detector(
        PersonalDataKind.CARD,
        _bounded(r"(?:[0-9]{12,19}|[0-9]{4}(?:[ -][0-9]{2,6}){2,4})"),
        _is_card,
    ),
    _Detector(
        PersonalDataKind.CPF,
        _bounded(r"[0-9]{3}[\s.-]?[0-9]{3}[\s.-]?[0-9]{3}[\s.-]?[0-9]{2}"),
        _is_cpf,
    ),
    # Could be a phone without area code; the value disappears either way, so the label is minor.
    _Detector(PersonalDataKind.POSTAL_CODE, _bounded(r"[0-9]{8}")),
    _Detector(PersonalDataKind.PHONE, _bounded(r"0[3589]00[\s-]?[0-9]{3}[\s-]?[0-9]{4}")),
    _Detector(
        PersonalDataKind.PHONE,
        _bounded(
            r"(?:\+?55[\s-]?)?(?:\(?0?[1-9][0-9]\)?[\s-]?)?(?:9[\s.-]?)?[0-9]{4}[\s.-]?[0-9]{4}"
        ),
    ),
    _Detector(PersonalDataKind.POSTAL_CODE, _bounded(r"[0-9]{2}\.?[0-9]{3}-[0-9]{3}")),
    _Detector(PersonalDataKind.ADDRESS, re.compile(_ADDRESS, re.IGNORECASE)),
)


def find_personal_data(normalized: str) -> tuple[PersonalDataMatch, ...]:
    """Recognized values in order of appearance. Expects the output of `normalize_text`.

    Blanking each value before the next pattern runs gives the same result as reading the text a
    second time after masking, which is what keeps masking idempotent.
    """
    working = normalized
    found: list[PersonalDataMatch] = []
    for detector in _DETECTORS:
        spans = [
            match.span()
            for match in detector.pattern.finditer(working)
            if detector.accepts(match.group())
        ]
        if detector.kind is not None:
            found.extend(PersonalDataMatch(detector.kind, start, end) for start, end in spans)
        working = _blank(working, spans)
    return tuple(sorted(found, key=lambda match: match.start))


def _blank(text: str, spans: list[tuple[int, int]]) -> str:
    parts: list[str] = []
    cursor = 0
    for start, end in spans:
        parts += [text[cursor:start], _BLANK * (end - start)]
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts)
