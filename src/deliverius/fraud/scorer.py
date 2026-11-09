"""The production scorer: a logistic regression and a sigmoid calibrator, applied without sklearn.

The research chose the logistic regression over LightGBM on the validation month (the rule in
`docs/antifraude.md`). The published artifact carries only numbers (medians, means, scales,
coefficients, the two calibrator parameters), so loading it runs no code from its author. Scoring is
27 multiply-adds: it runs inline, with no thread and no network.
"""

import math
import time
from dataclasses import dataclass

import structlog

from deliverius.fraud.decision import needs_review, top_factors
from deliverius.fraud.errors import FraudScoringError
from deliverius.fraud.features import FEATURE_NAMES, FeatureVector, compute_features
from deliverius.fraud.models import ClaimContext, FraudScore
from deliverius.shared.errors import InvariantError
from deliverius.shared.money import Money

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class LogisticModel:
    """`raw = intercept + sum(coef_j * z_j)`, with `z_j = (x_j or median_j - mean_j) / scale_j`.

    The same steps as the research pipeline (median imputer, standard scaler, logistic regression),
    so `coef_j * z_j` is the exact contribution of feature j to the raw score (log-odds).
    """

    medians: tuple[float, ...]
    means: tuple[float, ...]
    scales: tuple[float, ...]
    coefficients: tuple[float, ...]
    intercept: float

    def __post_init__(self) -> None:
        sizes = {len(self.medians), len(self.means), len(self.scales), len(self.coefficients)}
        if sizes != {len(FEATURE_NAMES)}:
            raise InvariantError("logistic_model.size_mismatch")
        numbers = (*self.medians, *self.means, *self.scales, *self.coefficients, self.intercept)
        if not all(math.isfinite(number) for number in numbers):
            raise InvariantError("logistic_model.not_finite")
        if any(scale <= 0 for scale in self.scales):
            raise InvariantError("logistic_model.scale_not_positive")

    def explain(self, vector: FeatureVector) -> tuple[float, tuple[float, ...]]:
        """The raw score (log-odds, before calibration) and the contribution of each feature."""
        contributions = tuple(
            coefficient * ((median if math.isnan(value) else value) - mean) / scale
            for value, median, mean, scale, coefficient in zip(
                vector, self.medians, self.means, self.scales, self.coefficients, strict=True
            )
        )
        return self.intercept + math.fsum(contributions), contributions


@dataclass(frozen=True, slots=True)
class SigmoidCalibrator:
    """Platt scaling as scikit-learn fits it: `p = 1 / (1 + exp(a * raw + b))`."""

    a: float
    b: float

    def __post_init__(self) -> None:
        if not (math.isfinite(self.a) and math.isfinite(self.b)):
            raise InvariantError("sigmoid_calibrator.not_finite")

    def probability(self, raw: float) -> float:
        exponent = self.a * raw + self.b
        # Two forms of the same value, so `exp` never overflows.
        if exponent >= 0:
            decay = math.exp(-exponent)
            return decay / (1.0 + decay)
        return 1.0 / (1.0 + math.exp(exponent))


@dataclass(frozen=True, slots=True)
class FraudModel:
    """A loaded and checked model: what `model_files.load_fraud_model` returns."""

    version: str
    logistic: LogisticModel
    calibrator: SigmoidCalibrator
    review_cost: Money


class LogisticFraudScorer:
    def __init__(self, model: FraudModel) -> None:
        self._model = model

    @property
    def model_version(self) -> str:
        return self._model.version

    async def score(self, context: ClaimContext) -> FraudScore:
        started = time.perf_counter()
        try:
            score = self._score(compute_features(context), context)
        except (ArithmeticError, ValueError) as error:
            logger.warning(
                "fraud.scoring_failed",
                model_version=self._model.version,
                order_id=str(context.order_id),
                error_type=type(error).__name__,
            )
            raise FraudScoringError from error
        logger.info(
            "fraud.scored",
            model_version=score.model_version,
            probability=round(score.probability, 4),
            needs_review=score.needs_review,
            action_kind=context.kind.value,
            order_id=str(context.order_id),
            latency_ms=round((time.perf_counter() - started) * 1000, 3),
        )
        return score

    def _score(self, vector: FeatureVector, context: ClaimContext) -> FraudScore:
        model = self._model
        raw, contributions = model.logistic.explain(vector)
        probability = model.calibrator.probability(raw)
        return FraudScore(
            probability=probability,
            model_version=model.version,
            needs_review=needs_review(probability, context.requested, model.review_cost),
            factors=top_factors(FEATURE_NAMES, contributions),
        )
