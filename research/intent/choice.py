"""The choice rule of design D2, written before any test number existed.

1. Eligible: recall of `sensitive` >= the floor over the three test sets together, and p95
   latency <= the ceiling, in CPU.
2. Best: the eligible approach with the highest macro F1 on the test written by people.
3. For every other eligible one, the paired, stratified bootstrap interval of F1(best) - F1(other)
   on the human test. An interval holding zero is a tie. The fastest (lowest p95) of the best and
   its ties wins; the size on disk breaks a latency tie.
4. Nothing eligible: no winner. Nothing is published and the decision goes back to the author.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from research.intent.config import RuleSection
from research.intent.metrics import Interval, Ints, macro_f1, paired_f1_difference


class Refusal(StrEnum):
    SENSITIVE_RECALL = "sensitive_recall"
    LATENCY = "p95_latency"


@dataclass(frozen=True, slots=True)
class Contender:
    name: str
    # Over the three test sets together.
    sensitive_recall: float
    p95_ms: float
    size_bytes: int
    human_predictions: Ints


@dataclass(frozen=True, slots=True)
class Choice:
    winner: str | None
    best: str | None
    eligible: tuple[str, ...]
    refused: dict[str, tuple[Refusal, ...]]
    human_f1: dict[str, float]
    # F1(best) - F1(other), for each other eligible approach.
    differences: dict[str, Interval]
    tied_with_best: tuple[str, ...]


def refusals(contender: Contender, rule: RuleSection) -> tuple[Refusal, ...]:
    found: list[Refusal] = []
    if not contender.sensitive_recall >= rule.min_sensitive_recall:
        found.append(Refusal.SENSITIVE_RECALL)
    if not contender.p95_ms <= rule.max_p95_ms:
        found.append(Refusal.LATENCY)
    return tuple(found)


def choose(
    contenders: Sequence[Contender], human_y: Ints, n_classes: int, rule: RuleSection, seed: int
) -> Choice:
    refused = {c.name: refusals(c, rule) for c in contenders}
    human_f1 = {c.name: macro_f1(human_y, c.human_predictions, n_classes) for c in contenders}
    eligible = [c for c in contenders if not refused[c.name]]
    if not eligible:
        return Choice(None, None, (), refused, human_f1, {}, ())

    def speed(contender: Contender) -> tuple[float, int, str]:
        return contender.p95_ms, contender.size_bytes, contender.name

    # An exact F1 tie for the best goes to the faster one already.
    best = min(eligible, key=lambda c: (-human_f1[c.name], *speed(c)))
    differences = {
        other.name: paired_f1_difference(
            human_y,
            best.human_predictions,
            other.human_predictions,
            n_classes,
            resamples=rule.bootstrap_resamples,
            seed=seed,
            level=rule.confidence_level,
        )
        for other in eligible
        if other is not best
    }
    tied = [c for c in eligible if c.name in differences and differences[c.name].contains_zero]
    winner = min([best, *tied], key=speed)
    return Choice(
        winner=winner.name,
        best=best.name,
        eligible=tuple(c.name for c in eligible),
        refused=refused,
        human_f1=human_f1,
        differences=differences,
        tied_with_best=tuple(c.name for c in tied),
    )
