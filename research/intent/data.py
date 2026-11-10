"""The messages every approach sees: the dataset checked against its manifest, then masked.

In the application `classify_intent` runs after `mask_input`, so the study trains and evaluates
on the output of `privacy.mask_text`, the same function, never on the raw text (design D1). Each
message is the first of its own conversation: an empty ledger named after the message.
"""

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

import numpy as np
import structlog
from numpy.typing import NDArray

from deliverius.datagen.output import read_records, verify_dataset
from deliverius.datagen.records import Intent, MessageRecord
from deliverius.privacy import MarkerLedger, mask_text

logger = structlog.get_logger(__name__)

# The order of the classes in every model: the label of a message is its position here.
INTENTS: Final = tuple(Intent)
LABELS: Final = tuple(intent.value for intent in INTENTS)
SENSITIVE: Final = INTENTS.index(Intent.SENSITIVE)
# Not a secret: the masked text does not depend on the key, only the ledger's fingerprints do,
# and the study throws the ledger away. A fixed key keeps every run identical.
MASK_KEY: Final = hashlib.sha256(b"deliverius-intent-research").digest()


class Corpus(StrEnum):
    TRAIN = "train"
    VALIDATION = "validation"
    TEST = "test"
    HOLDOUT = "holdout"
    HUMAN = "human"


SYNTHETIC_FILES: Final = {
    Corpus.TRAIN: "messages/train.jsonl",
    Corpus.VALIDATION: "messages/validation.jsonl",
    Corpus.TEST: "messages/test.jsonl",
    Corpus.HOLDOUT: "messages_holdout/test.jsonl",
}
TEST_SETS: Final = (Corpus.TEST, Corpus.HOLDOUT, Corpus.HUMAN)


@dataclass(frozen=True, slots=True)
class Example:
    id: str
    text: str
    intent: Intent
    has_injection: bool = False


@dataclass(frozen=True, slots=True)
class MessageSet:
    corpus: Corpus
    examples: tuple[Example, ...]

    def __len__(self) -> int:
        return len(self.examples)

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(example.id for example in self.examples)

    @property
    def texts(self) -> list[str]:
        return [example.text for example in self.examples]

    @property
    def labels(self) -> NDArray[np.int64]:
        return np.array([INTENTS.index(e.intent) for e in self.examples], dtype=np.int64)

    def subset(self, ids: Iterable[str]) -> "MessageSet":
        wanted = set(ids)
        return MessageSet(self.corpus, tuple(e for e in self.examples if e.id in wanted))


def mask(text: str, message_id: str) -> str:
    """What `classify_intent` receives in the application for the first message of a chat."""
    return mask_text(text, MarkerLedger.empty(message_id), MASK_KEY).text


def masked_examples(records: Sequence[MessageRecord]) -> tuple[Example, ...]:
    return tuple(
        Example(
            id=str(record.id),
            text=mask(record.text, str(record.id)),
            intent=record.intent,
            has_injection=record.has_injection,
        )
        for record in records
    )


def load_synthetic(directory: Path) -> dict[Corpus, MessageSet]:
    """Training, validation and the two synthetic test sets. A tampered file stops everything."""
    manifest = verify_dataset(directory)
    logger.info("intent_research.dataset_verified", datagen_version=manifest.datagen_version)
    return {
        corpus: MessageSet(corpus, masked_examples(read_records(directory, name, MessageRecord)))
        for corpus, name in SYNTHETIC_FILES.items()
    }
