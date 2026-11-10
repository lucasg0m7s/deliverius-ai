"""Small message datasets for the intent research tests: the real dataset is not in CI."""

import random
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from deliverius.datagen.output import write_dataset
from deliverius.datagen.records import Intent, Manifest, MessageRecord, PiiKind, PiiSpan, Split
from research.intent.data import SYNTHETIC_FILES, Corpus, Example, MessageSet

SPLIT_OF = {
    Corpus.TRAIN: Split.TRAIN,
    Corpus.VALIDATION: Split.VALIDATION,
    Corpus.TEST: Split.TEST,
    Corpus.HOLDOUT: Split.TEST,
}


def message(
    text: str,
    intent: Intent = Intent.OTHER,
    *,
    split: Split = Split.TRAIN,
    spans: Sequence[tuple[PiiKind, str]] = (),
    injection: bool = False,
) -> MessageRecord:
    """A record whose `pii_spans` point at each value, found in `text`."""
    return MessageRecord(
        id=uuid.uuid4(),
        scenario_id=uuid.uuid4(),
        split=split,
        text=text,
        intent=intent,
        pii_spans=tuple(
            PiiSpan(kind=kind, start=text.index(value), end=text.index(value) + len(value))
            for kind, value in spans
        ),
        has_injection=injection,
        persona="tone=calm",
    )


def write_messages(directory: Path, records: Mapping[Corpus, Sequence[MessageRecord]]) -> Path:
    """A dataset folder with the four message files (empty where not given) and its manifest."""
    write_dataset(
        directory,
        {name: list(records.get(corpus, ())) for corpus, name in SYNTHETIC_FILES.items()},
        Manifest(
            datagen_version="test",
            seed=1,
            commit="test",
            reference_instant=datetime(2026, 10, 1, tzinfo=UTC),
        ),
    )
    return directory


def message_set(
    corpus: Corpus, per_intent: int, *, intents: Sequence[Intent] = tuple(Intent)
) -> MessageSet:
    """`per_intent` examples of each intent, with ids `<corpus>-<intent>-<n>`."""
    return MessageSet(
        corpus,
        tuple(
            Example(id=f"{corpus}-{intent}-{n}", text=f"{intent} {n}", intent=intent)
            for intent in intents
            for n in range(per_intent)
        ),
    )


KEYWORDS = {
    Intent.ITEM_MISSING: "faltou",
    Intent.WRONG_ITEM: "trocado",
    Intent.QUALITY_ISSUE: "estragado",
    Intent.LATE_DELIVERY: "atrasou",
    Intent.NOT_DELIVERED: "sumiu",
    Intent.BILLING_ISSUE: "cobrança",
    Intent.CANCEL_ORDER: "cancelar",
    Intent.COUPON_QUESTION: "cupom",
    Intent.POLICY_QUESTION: "regra",
    Intent.SENSITIVE: "advogado",
    Intent.OTHER: "domingo",
}
FILLERS = ("oi", "olha", "boa noite", "por favor", "meu pedido", "hoje", "de novo", "aqui")


def learnable_set(corpus: Corpus, per_intent: int, seed: int = 0) -> MessageSet:
    """Messages whose intent is given away by one keyword, among random filler words."""
    rng = random.Random(f"{corpus}:{seed}")  # noqa: S311 - reproducible test texts, not security
    return MessageSet(
        corpus,
        tuple(
            Example(
                id=f"{corpus}-{intent}-{n}",
                text=" ".join([*rng.sample(FILLERS, 3), KEYWORDS[intent], *rng.sample(FILLERS, 2)]),
                intent=intent,
            )
            for intent in Intent
            for n in range(per_intent)
        ),
    )
