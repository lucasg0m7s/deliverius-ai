"""The metrics on small vectors whose answer is known by hand."""

from pathlib import Path

import numpy as np
import pytest

from research.fraud.evaluate import (
    brier,
    paired_pr_auc_difference,
    plot_precision_recall,
    plot_reliability,
    pr_auc,
    pr_auc_interval,
    recall_at_top,
    reliability,
    roc_auc,
    rule_cost,
)

Y = np.array([0, 0, 1, 0, 1, 0, 0, 0, 1, 0], dtype=np.int64)
PERFECT = np.array([0.1, 0.2, 0.9, 0.3, 0.8, 0.1, 0.2, 0.3, 0.95, 0.0])


def test_a_perfect_ranking_has_pr_auc_and_roc_auc_of_one() -> None:
    assert pr_auc(Y, PERFECT) == 1.0
    assert roc_auc(Y, PERFECT) == 1.0


def test_pr_auc_of_a_known_ranking() -> None:
    y = np.array([1, 0, 1, 0], dtype=np.int64)
    score = np.array([0.9, 0.8, 0.7, 0.1])

    # Precision 1 at the first fraud, 2/3 at the second: (1 + 2/3) / 2.
    assert pr_auc(y, score) == pytest.approx(5 / 6)


def test_brier_is_the_mean_squared_error_of_the_probability() -> None:
    y = np.array([1, 0], dtype=np.int64)

    assert brier(y, np.array([0.8, 0.4])) == pytest.approx((0.04 + 0.16) / 2)


def test_reliability_uses_bins_of_equal_size_by_probability() -> None:
    probability = np.linspace(0.05, 0.95, 20)
    y = (probability > 0.5).astype(np.int64)

    bins = reliability(y, probability, bins=10)

    assert [b.count for b in bins] == [2] * 10
    assert bins[0].mean_predicted == pytest.approx(probability[:2].mean())
    assert bins[0].observed_rate == 0.0
    assert bins[-1].observed_rate == 1.0


def test_recall_at_the_top_counts_frauds_among_the_most_suspicious() -> None:
    # 5% of 40 claims is 2: the two highest scores hold one of the two frauds.
    y = np.zeros(40, dtype=np.int64)
    y[[0, 30]] = 1
    score = np.linspace(1.0, 0.0, 40)

    assert recall_at_top(y, score, 0.05) == 0.5


def test_the_cost_of_a_rule_counts_reviews_and_missed_frauds() -> None:
    y = np.array([1, 1, 0, 0], dtype=np.int64)
    amounts = np.array([5_000, 300, 9_000, 100], dtype=np.int64)
    review = np.array([True, False, True, False])

    cost = rule_cost(y, amounts, review, review_cost_cents=400)

    assert (cost.reviewed, cost.frauds_caught, cost.total_cents) == (2, 1, 2 * 400 + 300)
    assert cost.per_claim(4) == pytest.approx(1_100 / 4)


def test_the_bootstrap_interval_holds_the_point_and_repeats_with_the_seed() -> None:
    rng = np.random.default_rng(1)
    y = (rng.random(300) < 0.2).astype(np.int64)
    score = y * 0.3 + rng.random(300)

    first = pr_auc_interval(y, score, resamples=200, seed=5)
    again = pr_auc_interval(y, score, resamples=200, seed=5)

    assert first == again
    assert first.low <= first.point <= first.high


def test_the_paired_difference_is_above_zero_only_for_a_clearly_better_score() -> None:
    rng = np.random.default_rng(2)
    y = (rng.random(400) < 0.2).astype(np.int64)
    good = y + rng.normal(0, 0.3, 400)
    noise = rng.random(400)

    better = paired_pr_auc_difference(y, good, noise, resamples=300, seed=3)
    same = paired_pr_auc_difference(y, noise, noise, resamples=300, seed=3)

    assert better.above_zero
    assert (same.point, same.low, same.high) == (0.0, 0.0, 0.0)
    assert not same.above_zero


def test_the_figures_are_written(tmp_path: Path) -> None:
    plot_reliability({"model": reliability(Y, PERFECT, bins=5)}, tmp_path / "rel.png")
    plot_precision_recall(Y, {"model": PERFECT}, tmp_path / "pr.png")

    assert (tmp_path / "rel.png").stat().st_size > 0
    assert (tmp_path / "pr.png").stat().st_size > 0
