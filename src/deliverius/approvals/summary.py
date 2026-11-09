"""The structured summary a handoff carries to the reviewer."""

import re
from dataclasses import dataclass
from typing import Final

from deliverius.privacy import find_personal_data, normalize_text
from deliverius.shared.errors import InvalidInputError

MAX_FACTS: Final = 10
MAX_FACT_LENGTH: Final = 300
MAX_ACTIONS_TAKEN: Final = 10
MAX_ACTION_TAKEN_LENGTH: Final = 200
_INTENT = re.compile(r"[a-z_]{1,40}")


@dataclass(frozen=True, slots=True)
class HandoffSummary:
    """Intent as a code, then short facts and the actions already taken, all bounded.

    The text comes from the agent and is stored and shown to a person, so the size is checked here,
    before any pattern runs over it; `personal_data_field` then looks for personal data.
    """

    intent: str
    facts: tuple[str, ...] = ()
    actions_taken: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not _INTENT.fullmatch(self.intent):
            raise InvalidInputError("handoff_summary.intent_format")
        _check_texts("facts", self.facts, MAX_FACTS, MAX_FACT_LENGTH)
        _check_texts(
            "actions_taken", self.actions_taken, MAX_ACTIONS_TAKEN, MAX_ACTION_TAKEN_LENGTH
        )

    def personal_data_field(self) -> str | None:
        """Where personal data was found (`facts[2]`), or `None`.

        Masking markers are not personal data.
        """
        for name, texts in (("facts", self.facts), ("actions_taken", self.actions_taken)):
            for index, text in enumerate(texts):
                if find_personal_data(normalize_text(text)):
                    return f"{name}[{index}]"
        return None


def _check_texts(name: str, texts: tuple[str, ...], max_count: int, max_length: int) -> None:
    if len(texts) > max_count:
        raise InvalidInputError(f"handoff_summary.{name}_too_many")
    if any(len(text) > max_length for text in texts):
        raise InvalidInputError(f"handoff_summary.{name}_too_long")
