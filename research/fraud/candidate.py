"""The chosen model as the application will load it: numbers in JSON, never a pickle.

`write_candidate` writes the folder atomically (a temporary folder renamed at the end), so a
crash never leaves a half-written candidate behind. `publish` later adds the card and manifest.
"""

import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel
from sklearn.calibration import CalibratedClassifierCV
from sklearn.pipeline import Pipeline

from deliverius.fraud.features import FEATURE_NAMES
from deliverius.fraud.model_files import (
    CALIBRATOR_FILE,
    DECISION_FILE,
    LOGISTIC_FILE,
    CalibratorDocument,
    DecisionDocument,
    LogisticDocument,
)
from deliverius.fraud.scorer import FraudModel, LogisticModel, SigmoidCalibrator
from deliverius.shared.money import Money

RESEARCH_FILE: Final = "research.json"
FIGURES_DIR: Final = "figures"
MODEL_FILES: Final = (LOGISTIC_FILE, CALIBRATOR_FILE, DECISION_FILE)


@dataclass(frozen=True)
class Candidate:
    logistic: LogisticDocument
    calibrator: CalibratorDocument
    decision: DecisionDocument

    def to_model(self, version: str) -> FraudModel:
        """The same object `load_fraud_model` builds from the published files."""
        return FraudModel(
            version=version,
            logistic=LogisticModel(
                medians=self.logistic.medians,
                means=self.logistic.means,
                scales=self.logistic.scales,
                coefficients=self.logistic.coefficients,
                intercept=self.logistic.intercept,
            ),
            calibrator=SigmoidCalibrator(self.calibrator.a, self.calibrator.b),
            review_cost=Money(self.decision.review_cost_cents),
        )


def candidate_from(
    pipeline: Pipeline, calibrated: CalibratedClassifierCV, review_cost_cents: int
) -> Candidate:
    imputer, scaler, model = (pipeline.named_steps[name] for name in ("impute", "scale", "model"))
    calibrator = calibrated.calibrated_classifiers_[0].calibrators[0]
    return Candidate(
        logistic=LogisticDocument(
            features=FEATURE_NAMES,
            medians=tuple(float(v) for v in imputer.statistics_),
            means=tuple(float(v) for v in scaler.mean_),
            scales=tuple(float(v) for v in scaler.scale_),
            coefficients=tuple(float(v) for v in model.coef_[0]),
            intercept=float(model.intercept_[0]),
        ),
        calibrator=CalibratorDocument(
            method="sigmoid", input="raw_score", a=float(calibrator.a_), b=float(calibrator.b_)
        ),
        decision=DecisionDocument(review_cost_cents=review_cost_cents),
    )


def canonical_json(document: BaseModel | dict[str, Any]) -> bytes:
    """Sorted keys, two-space indent, final newline: the same bytes for the same numbers."""
    data = document.model_dump(mode="json") if isinstance(document, BaseModel) else document
    return f"{json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False)}\n".encode()


def write_candidate(
    destination: Path,
    candidate: Candidate,
    research: dict[str, Any],
    figures: dict[str, Path],
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent))
    try:
        (staging / LOGISTIC_FILE).write_bytes(canonical_json(candidate.logistic))
        (staging / CALIBRATOR_FILE).write_bytes(canonical_json(candidate.calibrator))
        (staging / DECISION_FILE).write_bytes(canonical_json(candidate.decision))
        (staging / RESEARCH_FILE).write_bytes(canonical_json(research))
        (staging / FIGURES_DIR).mkdir()
        for name, path in figures.items():
            shutil.copyfile(path, staging / FIGURES_DIR / name)
        if destination.exists():
            shutil.rmtree(destination)
        staging.rename(destination)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def read_candidate(directory: Path) -> tuple[Candidate, dict[str, Any]]:
    candidate = Candidate(
        logistic=LogisticDocument.model_validate_json((directory / LOGISTIC_FILE).read_bytes()),
        calibrator=CalibratorDocument.model_validate_json(
            (directory / CALIBRATOR_FILE).read_bytes()
        ),
        decision=DecisionDocument.model_validate_json((directory / DECISION_FILE).read_bytes()),
    )
    research: dict[str, Any] = json.loads((directory / RESEARCH_FILE).read_bytes())
    return candidate, research
