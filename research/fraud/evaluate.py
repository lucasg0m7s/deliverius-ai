"""Metrics of a fraud score: ranking, calibration, the top of the queue and the cost of each rule.

The bootstrap is stratified (positives and negatives resampled apart), so every resample has
frauds and the PR-AUC is always defined; with about 60 frauds per month that matters.
"""

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import matplotlib as mpl

mpl.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
from numpy.typing import NDArray
from sklearn.metrics import (
    average_precision_score,
    precision_recall_curve,
    roc_auc_score,
)

type Floats = NDArray[np.float64]
type Labels = NDArray[np.int64]

RELIABILITY_BINS: Final = 10
TOP_FRACTION: Final = 0.05


@dataclass(frozen=True, slots=True)
class Interval:
    """A point estimate and its 95% bootstrap interval (2.5th and 97.5th percentiles)."""

    point: float
    low: float
    high: float

    @property
    def above_zero(self) -> bool:
        return self.low > 0


@dataclass(frozen=True, slots=True)
class ReliabilityBin:
    mean_predicted: float
    observed_rate: float
    count: int


@dataclass(frozen=True, slots=True)
class RuleCost:
    """Expected cost of a review rule, in cents.

    A review costs its price, a missed fraud costs its requested amount, the rest costs nothing.
    """

    reviewed: int
    frauds_caught: int
    total_cents: int

    def per_claim(self, claims: int) -> float:
        return self.total_cents / claims


def pr_auc(y: Labels, score: Floats) -> float:
    return float(average_precision_score(y, score))


def roc_auc(y: Labels, score: Floats) -> float:
    return float(roc_auc_score(y, score))


def brier(y: Labels, probability: Floats) -> float:
    return float(np.mean((probability - y) ** 2))


def reliability(
    y: Labels, probability: Floats, bins: int = RELIABILITY_BINS
) -> tuple[ReliabilityBin, ...]:
    """Bins of equal size by predicted probability (quantiles), lowest first."""
    order = np.argsort(probability, kind="stable")
    return tuple(
        ReliabilityBin(float(probability[rows].mean()), float(y[rows].mean()), len(rows))
        for rows in np.array_split(order, bins)
        if len(rows)
    )


def recall_at_top(y: Labels, score: Floats, fraction: float = TOP_FRACTION) -> float:
    """Share of all frauds found in the `fraction` most suspicious claims."""
    top = math.ceil(fraction * len(y))
    order = np.argsort(-score, kind="stable")[:top]
    return float(y[order].sum() / y.sum())


def rule_cost(
    y: Labels, requested_cents: NDArray[np.int64], review: NDArray[np.bool_], review_cost_cents: int
) -> RuleCost:
    missed = (~review) & (y == 1)
    return RuleCost(
        reviewed=int(review.sum()),
        frauds_caught=int((review & (y == 1)).sum()),
        total_cents=int(review.sum() * review_cost_cents + requested_cents[missed].sum()),
    )


def pr_auc_interval(y: Labels, score: Floats, resamples: int, seed: int) -> Interval:
    values = [pr_auc(y[rows], score[rows]) for rows in _stratified_resamples(y, resamples, seed)]
    return _interval(pr_auc(y, score), values)


def paired_pr_auc_difference(
    y: Labels, first: Floats, second: Floats, resamples: int, seed: int
) -> Interval:
    """PR-AUC(first) - PR-AUC(second) on the same resamples: the model choice rule."""
    values = [
        pr_auc(y[rows], first[rows]) - pr_auc(y[rows], second[rows])
        for rows in _stratified_resamples(y, resamples, seed)
    ]
    return _interval(pr_auc(y, first) - pr_auc(y, second), values)


def plot_reliability(curves: dict[str, tuple[ReliabilityBin, ...]], path: Path) -> None:
    figure, axes = plt.subplots(figsize=(5, 5))
    axes.plot([0, 1], [0, 1], linestyle="--", color="grey", label="perfect")
    for label, bins in curves.items():
        axes.plot(
            [b.mean_predicted for b in bins],
            [b.observed_rate for b in bins],
            marker="o",
            label=label,
        )
    axes.set(
        xlabel="mean predicted probability", ylabel="observed fraud rate", xlim=(0, 1), ylim=(0, 1)
    )
    axes.legend()
    figure.tight_layout()
    figure.savefig(path, dpi=120)
    plt.close(figure)


def plot_precision_recall(y: Labels, scores: dict[str, Floats], path: Path) -> None:
    figure, axes = plt.subplots(figsize=(5, 5))
    for label, score in scores.items():
        precision, recall, _ = precision_recall_curve(y, score)
        axes.step(recall, precision, where="post", label=f"{label} (AP {pr_auc(y, score):.3f})")
    axes.axhline(float(y.mean()), linestyle="--", color="grey", label="base rate")
    axes.set(xlabel="recall", ylabel="precision", xlim=(0, 1), ylim=(0, 1.02))
    axes.legend()
    figure.tight_layout()
    figure.savefig(path, dpi=120)
    plt.close(figure)


def _stratified_resamples(y: Labels, resamples: int, seed: int) -> list[NDArray[np.int64]]:
    rng = np.random.default_rng(seed)
    positives, negatives = np.flatnonzero(y == 1), np.flatnonzero(y == 0)
    return [
        np.concatenate(
            (
                rng.choice(positives, size=len(positives), replace=True),
                rng.choice(negatives, size=len(negatives), replace=True),
            )
        )
        for _ in range(resamples)
    ]


def _interval(point: float, values: list[float]) -> Interval:
    low, high = np.percentile(values, [2.5, 97.5])
    return Interval(point, float(low), float(high))
