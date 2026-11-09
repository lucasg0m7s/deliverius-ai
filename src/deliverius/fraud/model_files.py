"""The fraud model files: a pinned revision, a pinned manifest, and every file checked before use.

Loading goes in two steps. The manifest's size and SHA-256 are fixed here, so the manifest is
trusted only when it is the published one; then each file it lists is checked by its own size and
hash, and the feature schema it was trained on must be the schema of this code. Every file is
JSON with numbers only (no pickle): reading it runs no code. Changing the model is a pull request
that changes `REVISION` and `MANIFEST`, with the metrics comparison and the CI gate.
"""

from pathlib import Path
from typing import Final, Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from deliverius.artifacts import (
    ArtifactFileError,
    Downloader,
    ModelFile,
    PinnedRepository,
    fetch_files,
    verify_files,
)
from deliverius.fraud.errors import FraudModelError
from deliverius.fraud.features import FEATURE_NAMES, schema_hash
from deliverius.fraud.scorer import FraudModel, LogisticModel, SigmoidCalibrator
from deliverius.shared.money import Money

logger = structlog.get_logger(__name__)

MODEL_REPOSITORY: Final = "G0m7s/deliverius-fraud"
# Commit of the `v1.0.0` tag on the Hub; set together with `MANIFEST` when the model is published.
REVISION: Final = "0000000000000000000000000000000000000000"
REPOSITORY: Final = PinnedRepository(MODEL_REPOSITORY, REVISION)
MANIFEST: Final = ModelFile("manifest.json", 1, "0" * 64)

LOGISTIC_FILE: Final = "logistic.json"
CALIBRATOR_FILE: Final = "calibrator.json"
DECISION_FILE: Final = "decision.json"
CARD_FILE: Final = "README.md"


class _Document(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class ManifestFile(_Document):
    name: str
    size_bytes: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class DatasetLineage(_Document):
    repository: str
    version: str
    revision: str


class FraudManifest(_Document):
    """What the published model says about itself; `publish` writes it, `load` trusts it."""

    model_version: str = Field(min_length=1)
    model_kind: Literal["logistic_regression"]
    features_version: str
    feature_schema_hash: str
    dataset: DatasetLineage
    mlflow_run_id: str
    code_commit: str
    metrics: dict[str, dict[str, float]]
    files: tuple[ManifestFile, ...]

    def model_files(self) -> tuple[ModelFile, ...]:
        return tuple(ModelFile(f.name, f.size_bytes, f.sha256) for f in self.files)


class LogisticDocument(_Document):
    features: tuple[str, ...]
    medians: tuple[float, ...]
    means: tuple[float, ...]
    scales: tuple[float, ...]
    coefficients: tuple[float, ...]
    intercept: float


class CalibratorDocument(_Document):
    method: Literal["sigmoid"]
    # What the calibrator reads: the logistic regression's raw score (`decision_function`).
    input: Literal["raw_score"]
    a: float
    b: float


class DecisionDocument(_Document):
    review_cost_cents: int = Field(gt=0)


def load_fraud_model(
    directory: Path,
    repository: PinnedRepository = REPOSITORY,
    manifest_file: ModelFile = MANIFEST,
) -> FraudModel:
    """Check the manifest, then the files it lists, then the feature schema; then read the numbers.

    Raises `ArtifactFileError` naming a missing or altered file, or `FraudModelError` with a rule.
    """
    try:
        verify_files(repository, directory, (manifest_file,))
        manifest = _read(directory, manifest_file.name, FraudManifest)
        verify_files(repository, directory, manifest.model_files())
        _require_schema(manifest, manifest_file.name)
        model = _build(directory, manifest)
    except (ArtifactFileError, FraudModelError) as error:
        logger.warning("fraud.model_rejected", rule=error.rule, file=error.file)
        raise
    logger.info("fraud.model_loaded", model_version=model.version, repository=repository.repo_id)
    return model


def fetch_fraud_model(
    directory: Path,
    repository: PinnedRepository = REPOSITORY,
    manifest_file: ModelFile = MANIFEST,
    download: Downloader | None = None,
) -> tuple[str, ...]:
    """Download what is missing or wrong: the pinned manifest first, then the files it lists."""
    downloaded = fetch_files(repository, directory, (manifest_file,), download)
    manifest = _read(directory, manifest_file.name, FraudManifest)
    return downloaded + fetch_files(repository, directory, manifest.model_files(), download)


def _require_schema(manifest: FraudManifest, manifest_name: str) -> None:
    """A model trained on other features would read every value in the wrong place."""
    expected = schema_hash()
    if manifest.feature_schema_hash != expected:
        logger.warning(
            "fraud.schema_mismatch",
            expected_schema_hash=expected,
            manifest_schema_hash=manifest.feature_schema_hash,
        )
        raise FraudModelError("fraud_model.schema_mismatch", manifest_name)


def _build(directory: Path, manifest: FraudManifest) -> FraudModel:
    logistic = _read(directory, LOGISTIC_FILE, LogisticDocument)
    if logistic.features != FEATURE_NAMES:
        raise FraudModelError("fraud_model.feature_names_mismatch", LOGISTIC_FILE)
    calibrator = _read(directory, CALIBRATOR_FILE, CalibratorDocument)
    decision = _read(directory, DECISION_FILE, DecisionDocument)
    try:
        coefficients = LogisticModel(
            medians=logistic.medians,
            means=logistic.means,
            scales=logistic.scales,
            coefficients=logistic.coefficients,
            intercept=logistic.intercept,
        )
    except ValueError as error:
        raise FraudModelError("fraud_model.file_invalid", LOGISTIC_FILE) from error
    return FraudModel(
        version=manifest.model_version,
        logistic=coefficients,
        calibrator=SigmoidCalibrator(calibrator.a, calibrator.b),
        review_cost=Money(decision.review_cost_cents),
    )


def _read[T: BaseModel](directory: Path, name: str, document: type[T]) -> T:
    try:
        return document.model_validate_json((directory / name).read_bytes())
    except (OSError, ValidationError) as error:
        raise FraudModelError("fraud_model.file_invalid", name) from error
