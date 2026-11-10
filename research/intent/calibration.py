"""What every trained approach gets on validation: temperature, router threshold and a summary.

Only the validation set is used (design D3). The temperature is fitted by log-likelihood; the
threshold is the smallest confidence whose covered messages keep the error under the target.
"""

from dataclasses import dataclass
from typing import Any

from research.intent.config import IntentConfig
from research.intent.data import LABELS, SENSITIVE
from research.intent.metrics import (
    Floats,
    Ints,
    Predictions,
    coverage_at,
    expected_calibration_error,
    finite_or_none,
    fit_temperature,
    macro_f1,
    recall_of,
    router_threshold,
    softmax,
)


@dataclass(frozen=True, slots=True)
class Calibrated:
    temperature: float
    threshold: float | None
    summary: dict[str, Any]


def summarize(predictions: Predictions, y: Ints, config: IntentConfig) -> dict[str, Any]:
    correct = predictions.correct(y)
    return {
        "messages": len(y),
        "macro_f1": macro_f1(y, predictions.labels, len(LABELS)),
        "sensitive_recall": finite_or_none(recall_of(y, predictions.labels, SENSITIVE)),
        "ece": finite_or_none(
            expected_calibration_error(predictions.confidence, correct, config.calibration.ece_bins)
        ),
    }


def calibrate(validation_logits: Floats, y: Ints, config: IntentConfig) -> Calibrated:
    raw = Predictions.from_probabilities(softmax(validation_logits))
    temperature = fit_temperature(validation_logits, y, config.calibration.temperature_bounds)
    scaled = Predictions.from_probabilities(softmax(validation_logits, temperature))
    correct = scaled.correct(y)
    threshold = router_threshold(scaled.confidence, correct, config.router.max_error)
    covered = coverage_at(scaled.confidence, correct, threshold)
    summary = summarize(scaled, y, config) | {
        "ece_before_temperature": summarize(raw, y, config)["ece"],
        "temperature": temperature,
        "threshold": threshold,
        "coverage_at_threshold": covered.coverage,
        "error_at_threshold": finite_or_none(covered.error),
    }
    return Calibrated(temperature, threshold, summary)
