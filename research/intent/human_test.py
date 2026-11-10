"""The simulated-customer test (the `data/human-test/` format), read after `validate_human_test`.

The file is outside data: its size, rows and personal data are checked first, and its SHA-256
goes to MLflow, the report and the manifest, so a result always names the exact file. It is also
checked against training and validation: a message that leaked into them would inflate the score.
"""

import csv
import hashlib
import io
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import structlog

from deliverius.datagen.human_test import validate_human_test
from deliverius.datagen.records import Intent
from deliverius.privacy import normalize_text
from deliverius.shared.errors import DeliveriusError
from research.intent.data import Corpus, Example, MessageSet, mask

logger = structlog.get_logger(__name__)

_SPACES = re.compile(r"\s+")


class HumanTestError(DeliveriusError):
    """The test set cannot be used; `detail` names lines, intents or ids, never row content."""

    def __init__(self, rule: str, detail: str | None = None) -> None:
        super().__init__(rule if detail is None else f"{rule}: {detail}")
        self.rule = rule


@dataclass(frozen=True, slots=True)
class HumanTest:
    messages: MessageSet
    sha256: str


def load_human_test(path: Path, min_per_intent: int) -> HumanTest:
    if not path.is_file():
        raise HumanTestError("human_test.missing", str(path))
    report = validate_human_test(path)
    if not report.is_valid:
        lines = sorted({issue.line for issue in report.issues})
        raise HumanTestError("human_test.invalid", f"linhas {lines}")
    short = sorted(
        intent.value
        for intent in Intent
        if report.intent_counts.get(intent.value, 0) < min_per_intent
    )
    if short:
        raise HumanTestError("human_test.too_few_per_intent", ", ".join(short))
    content = path.read_bytes()
    examples = tuple(
        Example(
            id=row["id"].strip(),
            text=mask(row["text"].strip(), row["id"].strip()),
            intent=Intent(row["intent"].strip()),
        )
        for row in csv.DictReader(io.StringIO(content.decode("utf-8-sig"), newline=""))
    )
    sha256 = hashlib.sha256(content).hexdigest()
    logger.info("intent_research.human_test_verified", rows=len(examples), sha256=sha256)
    return HumanTest(MessageSet(Corpus.HUMAN, examples), sha256)


def comparable(text: str) -> str:
    """Normalized, case-folded and with single spaces: the form two copies of a message share."""
    return _SPACES.sub(" ", normalize_text(text)).strip().casefold()


def require_no_contamination(human: MessageSet, seen: Iterable[MessageSet]) -> None:
    """Refuses a human message whose text also appears in training or validation."""
    known = {comparable(example.text) for messages in seen for example in messages.examples}
    leaked = sorted(example.id for example in human.examples if comparable(example.text) in known)
    if leaked:
        raise HumanTestError("human_test.contaminated", ", ".join(leaked))
