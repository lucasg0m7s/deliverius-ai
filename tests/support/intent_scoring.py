"""Fake scorers for the evaluation and publication tests: keyword logits and a ticking clock."""

from collections.abc import Callable, Mapping, Sequence

import numpy as np
from support.intent_data import KEYWORDS

from deliverius.datagen.records import Intent
from research.intent.data import INTENTS, LABELS, TEST_SETS, Corpus, MessageSet
from research.intent.evaluate import Measured, measure_scorer


class KeywordScorer:
    """Knows every keyword, except the ones in `blind`, which it calls `other`."""

    def __init__(self, blind: frozenset[Intent] = frozenset()) -> None:
        self.blind = blind
        self.calls = 0

    def logits(self, texts: Sequence[str]) -> np.ndarray:
        self.calls += 1
        rows = np.zeros((len(texts), len(LABELS)))
        for row, text in enumerate(texts):
            intent = next(i for i, k in KEYWORDS.items() if k in text.split())
            rows[row, INTENTS.index(Intent.OTHER if intent in self.blind else intent)] = 6.0
        return rows


def ticking(milliseconds: float) -> Callable[[], float]:
    """A clock that advances `milliseconds` on every second read: one message, one duration."""
    state = {"now": 0.0, "reads": 0}

    def clock() -> float:
        state["reads"] += 1
        if state["reads"] % 2 == 0:
            state["now"] += milliseconds / 1000
        return state["now"]

    return clock


def measured(
    name: str,
    scorer: KeywordScorer,
    ms: float,
    tests: Mapping[Corpus, MessageSet],
    *,
    warmup: int = 20,
    size: int = 1_000,
) -> Measured:
    return measure_scorer(
        name,
        scorer,
        1.0,
        0.5,
        [tests[corpus] for corpus in TEST_SETS],
        warmup=warmup,
        size_bytes=size,
        clock=ticking(ms),
    )
