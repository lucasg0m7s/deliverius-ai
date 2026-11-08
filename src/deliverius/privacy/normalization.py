"""Text normalization: undoes the Unicode tricks that would hide a value from the patterns."""

import re
import unicodedata

_HORIZONTAL_SPACE = re.compile(r"[^\S\n]+")
_MINUS_SIGN = "\u2212"


def normalize_text(text: str) -> str:
    """Return the form every recognizer reads. Idempotent.

    NFKC alone is not enough: it keeps digits of other scripts and most dashes, which would slip
    past patterns written with `[0-9]` and `-`.
    """
    visible = "".join(char for char in text if unicodedata.category(char) != "Cf")
    composed = unicodedata.normalize("NFKC", visible)
    plain = "".join(_plain_char(char) for char in composed)
    return _HORIZONTAL_SPACE.sub(" ", plain)


def _plain_char(char: str) -> str:
    category = unicodedata.category(char)
    if category == "Nd":
        return str(unicodedata.digit(char))
    if category == "Pd" or char == _MINUS_SIGN:
        return "-"
    return char
