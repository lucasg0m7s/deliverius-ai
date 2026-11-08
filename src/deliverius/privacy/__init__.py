"""Privacy: finds personal data in free text and replaces it with markers, irreversibly.

Pure: no database, web framework, LLM or logging library. The first barrier before the LLM, the
database and the logs; `redact_text` is also the second barrier in the logger.
"""

from deliverius.privacy.detection import PersonalDataKind, PersonalDataMatch, find_personal_data
from deliverius.privacy.masking import (
    MAX_LEDGER_ENTRIES,
    MAX_TEXT_LENGTH,
    MIN_KEY_BYTES,
    TEXT_OMITTED,
    MarkerEntry,
    MarkerLedger,
    MaskedText,
    mask_text,
    redact_text,
)
from deliverius.privacy.normalization import normalize_text

__all__ = [
    "MAX_LEDGER_ENTRIES",
    "MAX_TEXT_LENGTH",
    "MIN_KEY_BYTES",
    "TEXT_OMITTED",
    "MarkerEntry",
    "MarkerLedger",
    "MaskedText",
    "PersonalDataKind",
    "PersonalDataMatch",
    "find_personal_data",
    "mask_text",
    "normalize_text",
    "redact_text",
]
