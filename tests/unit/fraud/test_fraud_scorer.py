"""The production scorer: contributions, calibration, the review rule and the scoring event."""

import math
from datetime import timedelta

import numpy as np
import pytest
from structlog.testing import capture_logs
from support.fraud_builders import (
    CLAIMED_AT,
    claim_ctx,
    claim_history,
    fraud_model,
    logistic_model,
    prior_claim,
)

from deliverius.fraud.errors import FraudScoringError
from deliverius.fraud.features import FEATURE_NAMES, compute_features
from deliverius.fraud.scorer import LogisticFraudScorer, LogisticModel, SigmoidCalibrator
from deliverius.shared.errors import InvariantError

SIZE = len(FEATURE_NAMES)
SUSPICIOUS = claim_ctx(
    history=claim_history(claims=tuple(prior_claim(day) for day in (1, 3, 5, 8, 13))),
    total_cents=9_000,
    requested_cents=9_000,
    delivered_at=CLAIMED_AT - timedelta(minutes=30),
)


def reference_raw(model: LogisticModel, vector: tuple[float, ...]) -> float:
    """The same model computed another way: numpy, as a matrix product."""
    values = np.array(vector)
    imputed = np.where(np.isnan(values), np.array(model.medians), values)
    standardized = (imputed - np.array(model.means)) / np.array(model.scales)
    return float(standardized @ np.array(model.coefficients) + model.intercept)


def test_contributions_plus_the_base_term_are_the_raw_score() -> None:
    model = logistic_model()
    vector = compute_features(SUSPICIOUS)

    raw, contributions = model.explain(vector)

    assert len(contributions) == SIZE
    assert raw == pytest.approx(model.intercept + sum(contributions), abs=1e-12)
    assert raw == pytest.approx(reference_raw(model, vector), abs=1e-6)


def test_a_missing_value_is_replaced_by_the_training_median() -> None:
    model = logistic_model()
    vector = compute_features(claim_ctx(delivered_at=None))
    position = FEATURE_NAMES.index("minutes_since_delivery")

    _, contributions = model.explain(vector)

    assert math.isnan(vector[position])
    assert contributions[position] == pytest.approx(0.1 * (1.0 - 0.5) / 2.0)


@pytest.mark.parametrize("raw", [-50.0, -1.0, 0.0, 0.3, 2.5, 50.0, 800.0, -800.0])
def test_the_calibrator_is_platt_scaling(raw: float) -> None:
    a, b = -1.3, 0.4
    exponent = a * raw + b
    expected = 0.0 if exponent > 700 else 1.0 / (1.0 + math.exp(exponent))

    assert SigmoidCalibrator(a, b).probability(raw) == pytest.approx(expected, abs=1e-12)


@pytest.mark.parametrize(("a", "b"), [(math.nan, 0.0), (0.0, math.inf)])
def test_a_calibrator_needs_finite_parameters(a: float, b: float) -> None:
    with pytest.raises(InvariantError, match=r"sigmoid_calibrator\.not_finite"):
        SigmoidCalibrator(a, b)


@pytest.mark.parametrize(
    ("change", "rule"),
    [
        ({"medians": (1.0,) * (SIZE - 1)}, "logistic_model.size_mismatch"),
        ({"intercept": math.nan}, "logistic_model.not_finite"),
        ({"scales": (0.0,) * SIZE}, "logistic_model.scale_not_positive"),
    ],
)
def test_a_logistic_model_refuses_impossible_numbers(change: dict[str, object], rule: str) -> None:
    fields: dict[str, object] = {
        "medians": (1.0,) * SIZE,
        "means": (0.0,) * SIZE,
        "scales": (1.0,) * SIZE,
        "coefficients": (0.0,) * SIZE,
        "intercept": 0.0,
    } | change

    with pytest.raises(InvariantError, match=rule):
        LogisticModel(**fields)  # type: ignore[arg-type]


async def test_a_suspicious_claim_gets_a_high_score_three_factors_and_a_review() -> None:
    scorer = LogisticFraudScorer(fraud_model())

    score = await scorer.score(SUSPICIOUS)

    assert score.probability > 0.5
    assert score.model_version == "1.0.0"
    assert score.needs_review is True
    assert len(score.factors) == 3
    weights = [abs(factor.contribution) for factor in score.factors]
    assert weights == sorted(weights, reverse=True)
    assert score.factors[0].feature == "claims_30d"


async def test_the_probability_is_the_calibrated_raw_score() -> None:
    model = fraud_model(a=-0.8, b=0.2)
    raw, _ = model.logistic.explain(compute_features(SUSPICIOUS))

    score = await LogisticFraudScorer(model).score(SUSPICIOUS)

    assert score.probability == pytest.approx(1 / (1 + math.exp(-0.8 * raw + 0.2)), abs=1e-12)


async def test_the_review_follows_the_cost_rule_of_the_artifact() -> None:
    cheap = claim_ctx(requested_cents=100)
    model = fraud_model(review_cost_cents=400)

    score = await LogisticFraudScorer(model).score(cheap)

    assert score.needs_review is (score.probability * 100 >= 400)
    assert score.needs_review is False


async def test_a_score_logs_one_event_without_personal_data_or_history_values() -> None:
    with capture_logs() as logs:
        await LogisticFraudScorer(fraud_model()).score(SUSPICIOUS)

    assert [log["event"] for log in logs] == ["fraud.scored"]
    event = logs[0]
    assert set(event) == {
        "event",
        "log_level",
        "model_version",
        "probability",
        "needs_review",
        "action_kind",
        "order_id",
        "latency_ms",
    }
    assert event["order_id"] == str(SUSPICIOUS.order_id)
    assert event["action_kind"] == "refund"
    assert event["latency_ms"] >= 0


async def test_a_model_that_overflows_raises_a_scoring_error() -> None:
    huge = logistic_model(coefficients=(1e308,) * SIZE)

    with capture_logs() as logs, pytest.raises(FraudScoringError):
        await LogisticFraudScorer(fraud_model(huge)).score(SUSPICIOUS)

    assert logs[0]["event"] == "fraud.scoring_failed"


def test_the_scorer_names_its_model_version() -> None:
    assert LogisticFraudScorer(fraud_model(version="2.0.0")).model_version == "2.0.0"
