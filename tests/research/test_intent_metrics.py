"""The study's measures on small vectors whose results are known by hand."""

import math

import numpy as np
import pytest

from research.intent.metrics import (
    EMPTY,
    CoveragePoint,
    Predictions,
    confusion_matrix,
    coverage_at,
    coverage_curve,
    expected_calibration_error,
    f1_interval,
    finite_or_none,
    fit_temperature,
    latency_percentiles,
    macro_f1,
    negative_log_likelihood,
    paired_f1_difference,
    recall_by_class,
    recall_of,
    router_threshold,
    softmax,
)

Y = np.array([0, 0, 1, 1], dtype=np.int64)


def ints(*values: int) -> np.ndarray:
    return np.array(values, dtype=np.int64)


def test_perfect_predictions_have_f1_one() -> None:
    assert macro_f1(Y, Y, 2) == 1.0


def test_macro_f1_by_hand() -> None:
    # Class 0: tp 1, fp 0, fn 1 -> 2/3. Class 1: tp 2, fp 1, fn 0 -> 4/5.
    assert macro_f1(Y, ints(0, 1, 1, 1), 2) == pytest.approx((2 / 3 + 4 / 5) / 2)


def test_an_empty_prediction_is_an_error_of_its_true_class_only() -> None:
    # Class 0: tp 1, fn 1 -> 2/3. Class 1: perfect -> 1. No false positive anywhere.
    assert macro_f1(Y, ints(0, EMPTY, 1, 1), 2) == pytest.approx((2 / 3 + 1) / 2)
    assert recall_of(Y, ints(0, EMPTY, 1, 1), 0) == 0.5


def test_a_class_absent_from_truth_and_predictions_scores_zero() -> None:
    assert macro_f1(Y, Y, 3) == pytest.approx(2 / 3)


def test_recall_by_class_and_of_an_absent_class() -> None:
    assert recall_by_class(Y, ints(0, 1, 1, 1), 2) == [0.5, 1.0]
    assert math.isnan(recall_of(Y, Y, 5))


def test_the_confusion_matrix_counts_empties_in_the_last_column() -> None:
    assert confusion_matrix(Y, ints(0, EMPTY, 0, 1), 2).tolist() == [[1, 0, 1], [1, 1, 0]]


def test_the_bootstrap_interval_holds_the_point_and_repeats_with_the_seed() -> None:
    rng = np.random.default_rng(0)
    y = rng.integers(0, 3, 200).astype(np.int64)
    predicted = np.where(rng.random(200) < 0.8, y, (y + 1) % 3).astype(np.int64)

    interval = f1_interval(y, predicted, 3, resamples=300, seed=1)

    assert interval.low <= interval.point <= interval.high
    assert interval.high - interval.low < 0.2
    assert interval == f1_interval(y, predicted, 3, resamples=300, seed=1)


def test_identical_models_have_a_zero_paired_difference() -> None:
    difference = paired_f1_difference(Y, Y, Y, 2, resamples=50, seed=1)

    assert (difference.point, difference.low, difference.high) == (0.0, 0.0, 0.0)
    assert difference.contains_zero


def test_a_clearly_better_model_has_a_difference_above_zero() -> None:
    rng = np.random.default_rng(0)
    y = rng.integers(0, 2, 300).astype(np.int64)
    worse = np.where(rng.random(300) < 0.6, y, 1 - y).astype(np.int64)

    difference = paired_f1_difference(y, y, worse, 2, resamples=300, seed=1)

    assert difference.low > 0
    assert not difference.contains_zero


def test_ece_by_hand() -> None:
    confidence = np.array([0.95, 0.95, 0.35, 0.35])
    correct = np.array([True, False, True, True])

    # Bin of 0.95: accuracy 0.5, gap 0.45. Bin of 0.35: accuracy 1, gap 0.65. Half each.
    assert expected_calibration_error(confidence, correct, 15) == pytest.approx(0.55)


def test_ece_puts_confidence_one_in_the_last_bin_and_zero_in_the_first() -> None:
    assert expected_calibration_error(np.array([1.0, 0.0]), np.array([True, False]), 15) == 0.0
    assert math.isnan(expected_calibration_error(np.array([]), np.array([], dtype=bool)))


def test_softmax_rows_sum_to_one_and_temperature_flattens() -> None:
    logits = np.array([[2.0, 0.0], [0.0, 0.0]])

    sharp, flat = softmax(logits), softmax(logits, temperature=4.0)

    assert sharp.sum(axis=1) == pytest.approx([1.0, 1.0])
    assert flat[0, 0] < sharp[0, 0]
    assert sharp[1].tolist() == [0.5, 0.5]


def test_the_temperature_softens_an_overconfident_model() -> None:
    rng = np.random.default_rng(0)
    y = rng.integers(0, 2, 500).astype(np.int64)
    right = rng.random(500) < 0.7
    predicted = np.where(right, y, 1 - y)
    logits = np.zeros((500, 2))
    logits[np.arange(500), predicted] = 8.0  # says ~100% and is right 70% of the time

    temperature = fit_temperature(logits, y, (0.05, 20.0))

    assert temperature > 1
    assert negative_log_likelihood(logits, y, temperature) < negative_log_likelihood(logits, y, 1)


def test_predictions_from_probabilities() -> None:
    predictions = Predictions.from_probabilities(np.array([[0.2, 0.8], [0.6, 0.4]]))

    assert predictions.labels.tolist() == [1, 0]
    assert predictions.confidence.tolist() == [0.8, 0.6]
    assert predictions.correct(ints(1, 1)).tolist() == [True, False]
    assert predictions.subset(ints(1)).labels.tolist() == [0]


CONFIDENCE = np.array([0.9, 0.8, 0.8, 0.6, 0.3])
CORRECT = np.array([True, True, False, True, False])


def test_the_coverage_curve_keeps_ties_together() -> None:
    assert coverage_curve(CONFIDENCE, CORRECT) == [
        CoveragePoint(0.9, 0.2, 0.0),
        CoveragePoint(0.8, 0.6, 1 / 3),
        CoveragePoint(0.6, 0.8, 0.25),
        CoveragePoint(0.3, 1.0, 0.4),
    ]


def test_the_router_takes_the_smallest_threshold_within_the_error() -> None:
    assert router_threshold(CONFIDENCE, CORRECT, max_error=0.05) == 0.9
    assert router_threshold(CONFIDENCE, CORRECT, max_error=0.25) == 0.6
    assert router_threshold(CONFIDENCE, CORRECT, max_error=0.5) == 0.3


def test_no_threshold_within_the_error_routes_everything_to_a_question() -> None:
    assert router_threshold(CONFIDENCE, np.zeros(5, dtype=bool), max_error=0.05) is None
    assert router_threshold(np.array([]), np.array([], dtype=bool), 0.05) is None


def test_coverage_and_error_at_a_threshold() -> None:
    assert coverage_at(CONFIDENCE, CORRECT, 0.6) == CoveragePoint(0.6, 0.8, 0.25)
    assert coverage_at(CONFIDENCE, CORRECT, None).coverage == 0.0
    assert math.isnan(coverage_at(CONFIDENCE, CORRECT, 0.95).error)


def test_nan_becomes_none_for_json() -> None:
    assert finite_or_none(float("nan")) is None
    assert finite_or_none(None) is None
    assert finite_or_none(0.5) == 0.5


def test_latency_percentiles() -> None:
    assert latency_percentiles(np.arange(1, 101, dtype=np.float64)) == pytest.approx((50.5, 95.05))
