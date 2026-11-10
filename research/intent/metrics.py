"""The measures of the study, the same code for the four approaches (design D4 and D11).

A prediction is a label and a confidence. An empty prediction (the LLM failed, refused or
answered outside the classes) has the label `EMPTY` and confidence 0: it counts as an error, it
is never dropped. Intervals come from a bootstrap stratified by the true class, with a fixed seed.
"""

import math
from dataclasses import dataclass
from typing import Final

import numpy as np
from numpy.typing import NDArray
from scipy.optimize import minimize_scalar

type Floats = NDArray[np.float64]
type Ints = NDArray[np.int64]

EMPTY: Final = -1


@dataclass(frozen=True, slots=True)
class Interval:
    point: float
    low: float
    high: float

    @property
    def contains_zero(self) -> bool:
        return self.low <= 0 <= self.high

    def as_dict(self) -> dict[str, float]:
        return {"point": self.point, "low": self.low, "high": self.high}


@dataclass(frozen=True, slots=True)
class Predictions:
    labels: Ints
    confidence: Floats

    @classmethod
    def from_probabilities(cls, probabilities: Floats) -> "Predictions":
        return cls(
            labels=probabilities.argmax(axis=1).astype(np.int64),
            confidence=probabilities.max(axis=1).astype(np.float64),
        )

    def correct(self, y: Ints) -> NDArray[np.bool_]:
        hits: NDArray[np.bool_] = self.labels == y
        return hits

    def subset(self, rows: Ints) -> "Predictions":
        return Predictions(self.labels[rows], self.confidence[rows])


def macro_f1(y: Ints, predicted: Ints, n_classes: int) -> float:
    """Mean F1 over every class; an empty prediction is a miss of its true class."""
    valid = predicted != EMPTY
    true_positive = np.bincount(y[valid & (predicted == y)], minlength=n_classes)
    predicted_count = np.bincount(predicted[valid], minlength=n_classes)
    actual_count = np.bincount(y, minlength=n_classes)
    denominator = predicted_count + actual_count
    scores = np.divide(
        2 * true_positive,
        denominator,
        out=np.zeros(n_classes, dtype=np.float64),
        where=denominator > 0,
    )
    return float(scores.mean())


def recall_of(y: Ints, predicted: Ints, label: int) -> float:
    members = y == label
    return float((predicted[members] == label).mean()) if members.any() else float("nan")


def recall_by_class(y: Ints, predicted: Ints, n_classes: int) -> list[float]:
    return [recall_of(y, predicted, label) for label in range(n_classes)]


def confusion_matrix(y: Ints, predicted: Ints, n_classes: int) -> Ints:
    """Rows are true classes; the last column counts the empty predictions."""
    columns = np.where(predicted == EMPTY, n_classes, predicted)
    matrix = np.zeros((n_classes, n_classes + 1), dtype=np.int64)
    np.add.at(matrix, (y, columns), 1)
    return matrix


def _stratified_resamples(y: Ints, resamples: int, seed: int) -> list[Ints]:
    rng = np.random.default_rng(seed)
    groups = [np.flatnonzero(y == label) for label in np.unique(y)]
    return [
        np.concatenate([rng.choice(group, size=len(group)) for group in groups])
        for _ in range(resamples)
    ]


def _interval(point: float, samples: list[float], level: float) -> Interval:
    tail = (1 - level) / 2 * 100
    low, high = np.percentile(samples, [tail, 100 - tail])
    return Interval(point, float(low), float(high))


def f1_interval(
    y: Ints, predicted: Ints, n_classes: int, *, resamples: int, seed: int, level: float = 0.95
) -> Interval:
    samples = [
        macro_f1(y[rows], predicted[rows], n_classes)
        for rows in _stratified_resamples(y, resamples, seed)
    ]
    return _interval(macro_f1(y, predicted, n_classes), samples, level)


def paired_f1_difference(
    y: Ints,
    first: Ints,
    second: Ints,
    n_classes: int,
    *,
    resamples: int,
    seed: int,
    level: float = 0.95,
) -> Interval:
    """F1(first) - F1(second), both on the same resampled messages each time."""

    def difference(rows: Ints) -> float:
        return macro_f1(y[rows], first[rows], n_classes) - macro_f1(
            y[rows], second[rows], n_classes
        )

    everything = np.arange(len(y))
    samples = [difference(rows) for rows in _stratified_resamples(y, resamples, seed)]
    return _interval(difference(everything), samples, level)


def expected_calibration_error(
    confidence: Floats, correct: NDArray[np.bool_], bins: int = 15
) -> float:
    """Equal-width bins over the confidence of the predicted class, weighted by their size."""
    if len(confidence) == 0:
        return float("nan")
    index = np.minimum((confidence * bins).astype(np.int64), bins - 1)
    total = 0.0
    for bin_index in range(bins):
        members = index == bin_index
        if members.any():
            gap = abs(float(correct[members].mean()) - float(confidence[members].mean()))
            total += members.sum() / len(confidence) * gap
    return total


def softmax(logits: Floats, temperature: float = 1.0) -> Floats:
    scaled = logits / temperature
    shifted = np.exp(scaled - scaled.max(axis=1, keepdims=True))
    probabilities: Floats = shifted / shifted.sum(axis=1, keepdims=True)
    return probabilities


def negative_log_likelihood(logits: Floats, y: Ints, temperature: float) -> float:
    scaled = logits / temperature
    shifted = scaled - scaled.max(axis=1, keepdims=True)
    log_probabilities = shifted - np.log(np.exp(shifted).sum(axis=1, keepdims=True))
    return float(-log_probabilities[np.arange(len(y)), y].mean())


def fit_temperature(logits: Floats, y: Ints, bounds: tuple[float, float]) -> float:
    """The temperature with the lowest negative log-likelihood, by bounded scalar search."""

    def objective(temperature: float) -> float:
        return negative_log_likelihood(logits, y, temperature)

    result = minimize_scalar(objective, bounds=bounds, method="bounded", options={"xatol": 1e-6})
    return float(result.x)


@dataclass(frozen=True, slots=True)
class CoveragePoint:
    threshold: float
    coverage: float
    error: float


def coverage_curve(confidence: Floats, correct: NDArray[np.bool_]) -> list[CoveragePoint]:
    """For each distinct confidence `t`: the share of messages with confidence >= `t`, and the
    error among them. Ordered from the highest threshold (least coverage) to the lowest."""
    if len(confidence) == 0:
        return []
    order = np.argsort(-confidence, kind="stable")
    ordered, hits = confidence[order], correct[order]
    errors = np.cumsum(~hits)
    # The last position of each run of equal confidences: a threshold takes the whole tie.
    ends = np.flatnonzero(np.append(ordered[1:] != ordered[:-1], True))
    return [
        CoveragePoint(
            threshold=float(ordered[end]),
            coverage=(end + 1) / len(ordered),
            error=float(errors[end]) / (end + 1),
        )
        for end in ends
    ]


def router_threshold(
    confidence: Floats, correct: NDArray[np.bool_], max_error: float
) -> float | None:
    """The smallest threshold whose covered messages have an error at or below `max_error`.

    `None` when no threshold reaches it: then every message goes to the clarifying question.
    """
    passing = [
        point.threshold for point in coverage_curve(confidence, correct) if point.error <= max_error
    ]
    return min(passing) if passing else None


def coverage_at(
    confidence: Floats, correct: NDArray[np.bool_], threshold: float | None
) -> CoveragePoint:
    """Coverage and error of the messages the router would accept with `threshold`."""
    if threshold is None or len(confidence) == 0:
        return CoveragePoint(threshold=float("nan"), coverage=0.0, error=float("nan"))
    covered = confidence >= threshold
    error = float((~correct[covered]).mean()) if covered.any() else float("nan")
    return CoveragePoint(threshold, float(covered.mean()), error)


def finite_or_none(value: float | None) -> float | None:
    """For JSON: NaN (nothing to measure) becomes `None`, which JSON can hold and compare."""
    return None if value is None or math.isnan(value) else value


def latency_percentiles(milliseconds: Floats) -> tuple[float, float]:
    p50, p95 = np.percentile(milliseconds, [50, 95])
    return float(p50), float(p95)
