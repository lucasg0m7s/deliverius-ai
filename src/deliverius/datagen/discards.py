"""Counts of discarded variations, by labels (a reason, and for messages an intent).

Only counts and labels, never text. A tally can be written as JSON, so the attempt log keeps the
discards of the attempts before the one that worked, and a repeated run adds them back.
"""

import json
from collections import Counter
from collections.abc import Mapping
from typing import Any, Self


class DiscardTally:
    def __init__(self) -> None:
        self._counts: Counter[tuple[str, ...]] = Counter()

    @property
    def counts(self) -> Mapping[tuple[str, ...], int]:
        return self._counts

    def add(self, *labels: str) -> None:
        self._counts[labels] += 1

    def merge(self, other: Self) -> None:
        self._counts.update(other.counts)

    def to_json(self) -> str:
        rows = [[*labels, count] for labels, count in sorted(self._counts.items())]
        return json.dumps(rows)

    @classmethod
    def from_json(cls, text: str) -> Self:
        tally = cls()
        rows: list[list[Any]] = json.loads(text)
        for *labels, count in rows:
            tally._counts[tuple(labels)] = count
        return tally
