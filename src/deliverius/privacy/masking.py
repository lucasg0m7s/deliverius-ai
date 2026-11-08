"""Replaces personal data with markers, without keeping any way back to the value."""

import hashlib
import hmac
import re
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Self

from deliverius.privacy.detection import PersonalDataKind, PersonalDataMatch, find_personal_data
from deliverius.privacy.normalization import normalize_text
from deliverius.shared.errors import InvariantError

MAX_TEXT_LENGTH = 64_000
MAX_LEDGER_ENTRIES = 100
MIN_KEY_BYTES = 32
TEXT_OMITTED = "[TEXTO_OMITIDO]"

_FINGERPRINT_LENGTH = 32
_PUNCTUATION = re.compile(r"\W")
# `+55 11 91234-5678`, `011 91234-5678` and `11912345678` are the same phone.
_PHONE_PREFIX = re.compile(r"^(?:55(?=[0-9]{10,11}$))?(?:0(?=[1-9][0-9]{9,10}$))?")


@dataclass(frozen=True, slots=True)
class MarkerEntry:
    fingerprint: str
    kind: PersonalDataKind
    number: int


@dataclass(frozen=True, slots=True)
class MarkerLedger:
    """What one conversation has seen: fingerprints only, never a value.

    Immutable, so it serializes into the graph state and two readers cannot race on it.
    """

    conversation_id: str
    entries: tuple[MarkerEntry, ...] = ()

    @classmethod
    def empty(cls, conversation_id: str) -> Self:
        return cls(conversation_id)


@dataclass(frozen=True, slots=True)
class MaskedText:
    text: str
    ledger: MarkerLedger
    counts: Mapping[PersonalDataKind, int]


def redact_text(text: str) -> str:
    """Stateless masking with unnumbered markers: for logs and traces, with no conversation."""
    normalized = _normalize_within_limit(text)
    if normalized is None:
        return TEXT_OMITTED
    matches = find_personal_data(normalized)
    return _replace(normalized, matches, _bare_marker)


def mask_text(text: str, ledger: MarkerLedger, key: bytes) -> MaskedText:
    """Masks a conversation message; the same value keeps its marker across turns.

    The key keeps the fingerprints from being brute-forced: there are only 10^9 CPFs.
    """
    if len(key) < MIN_KEY_BYTES:
        raise InvariantError("masking.key_too_short")
    normalized = _normalize_within_limit(text)
    if normalized is None:
        return MaskedText(TEXT_OMITTED, ledger, {})

    known = {entry.fingerprint: entry for entry in ledger.entries}

    def marker_for(match: PersonalDataMatch) -> str:
        value = normalized[match.start : match.end]
        fingerprint = _fingerprint(key, ledger.conversation_id, match.kind, value)
        entry = known.get(fingerprint)
        if entry is None:
            if len(known) >= MAX_LEDGER_ENTRIES:
                return _bare_marker(match)
            number = 1 + sum(1 for seen in known.values() if seen.kind is match.kind)
            entry = MarkerEntry(fingerprint, match.kind, number)
            known[fingerprint] = entry
        return f"[{entry.kind.value}_{entry.number}]"

    matches = find_personal_data(normalized)
    masked = _replace(normalized, matches, marker_for)
    return MaskedText(
        masked,
        replace(ledger, entries=tuple(known.values())),
        Counter(match.kind for match in matches),
    )


def _normalize_within_limit(text: str) -> str | None:
    """Checked twice because NFKC can multiply the length by 18 (U+FDFA)."""
    if len(text) > MAX_TEXT_LENGTH:
        return None
    normalized = normalize_text(text)
    return normalized if len(normalized) <= MAX_TEXT_LENGTH else None


def _replace(
    text: str,
    matches: Sequence[PersonalDataMatch],
    marker_for: Callable[[PersonalDataMatch], str],
) -> str:
    parts: list[str] = []
    cursor = 0
    for match in matches:
        parts += [text[cursor : match.start], marker_for(match)]
        cursor = match.end
    parts.append(text[cursor:])
    return "".join(parts)


def _bare_marker(match: PersonalDataMatch) -> str:
    return f"[{match.kind.value}]"


def _fingerprint(key: bytes, conversation_id: str, kind: PersonalDataKind, value: str) -> str:
    """The conversation enters the hash so the ledger cannot link two conversations."""
    message = "\x00".join((conversation_id, kind.value, _canonical(kind, value)))
    digest = hmac.new(key, message.encode("utf-8", "surrogatepass"), hashlib.sha256)
    return digest.hexdigest()[:_FINGERPRINT_LENGTH]


def _canonical(kind: PersonalDataKind, value: str) -> str:
    compact = _PUNCTUATION.sub("", value).casefold()
    return _PHONE_PREFIX.sub("", compact) if kind is PersonalDataKind.PHONE else compact
