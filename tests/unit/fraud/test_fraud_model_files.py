"""Loading the fraud model: pinned manifest, listed files, feature schema, then the numbers."""

import hashlib
import json
from pathlib import Path

import pytest
from structlog.testing import capture_logs
from support.fraud_artifact import (
    TEST_REPOSITORY,
    WrittenArtifact,
    artifact_contents,
    write_artifact,
)
from support.fraud_builders import claim_ctx

from deliverius.artifacts import ArtifactFileError, ModelFile
from deliverius.fraud.errors import FraudModelError
from deliverius.fraud.features import FEATURE_NAMES, schema_hash
from deliverius.fraud.model_files import (
    MANIFEST,
    MODEL_REPOSITORY,
    REPOSITORY,
    REVISION,
    fetch_fraud_model,
    load_fraud_model,
)
from deliverius.fraud.scorer import LogisticFraudScorer
from deliverius.shared.money import Money


def load(artifact: WrittenArtifact) -> object:
    return load_fraud_model(artifact.directory, TEST_REPOSITORY, artifact.manifest)


class Hub:
    """Serves the files of an artifact, recording what was asked."""

    def __init__(self, contents: dict[str, bytes]) -> None:
        self.contents = contents
        self.downloaded: list[str] = []

    def __call__(self, file: ModelFile, directory: Path) -> None:
        self.downloaded.append(file.name)
        (directory / file.name).write_bytes(self.contents[file.name])


def test_the_pins_name_the_published_repository() -> None:
    assert MODEL_REPOSITORY == "G0m7s/deliverius-fraud"
    assert REPOSITORY.revision == REVISION
    assert len(REVISION) == 40
    assert MANIFEST.name == "manifest.json"
    assert len(MANIFEST.sha256) == 64


async def test_a_checked_artifact_loads_into_a_working_scorer(tmp_path: Path) -> None:
    artifact = write_artifact(tmp_path)

    with capture_logs() as logs:
        model = load_fraud_model(tmp_path, TEST_REPOSITORY, artifact.manifest)

    assert model.version == "1.0.0"
    assert model.review_cost == Money(400)
    assert (model.calibrator.a, model.calibrator.b) == (-1.1, 0.3)
    assert model.logistic.coefficients == (0.1,) * len(FEATURE_NAMES)
    assert logs[-1]["event"] == "fraud.model_loaded"
    score = await LogisticFraudScorer(model).score(claim_ctx())
    assert 0.0 <= score.probability <= 1.0


def test_a_model_file_swapped_for_one_of_the_same_size_is_refused(tmp_path: Path) -> None:
    artifact = write_artifact(tmp_path)
    original = artifact.contents["logistic.json"]
    (tmp_path / "logistic.json").write_bytes(original.replace(b"-2.0", b"-3.0"))

    with capture_logs() as logs, pytest.raises(ArtifactFileError) as raised:
        load(artifact)

    assert raised.value.rule == "model_file.hash_mismatch"
    assert raised.value.file == "logistic.json"
    assert logs[-1] == {
        "event": "fraud.model_rejected",
        "log_level": "warning",
        "rule": "model_file.hash_mismatch",
        "file": "logistic.json",
    }


@pytest.mark.parametrize("name", ["manifest.json", "calibrator.json", "decision.json"])
def test_a_missing_file_is_refused_naming_it(tmp_path: Path, name: str) -> None:
    artifact = write_artifact(tmp_path)
    (tmp_path / name).unlink()

    with pytest.raises(ArtifactFileError) as raised:
        load(artifact)

    assert (raised.value.rule, raised.value.file) == ("model_file.missing", name)


def test_a_manifest_other_than_the_pinned_one_is_refused(tmp_path: Path) -> None:
    artifact = write_artifact(tmp_path)
    tampered = json.loads(artifact.contents["manifest.json"])
    tampered["model_version"] = "9.9.9"
    (tmp_path / "manifest.json").write_text(json.dumps(tampered), encoding="utf-8")

    with pytest.raises(ArtifactFileError) as raised:
        load(artifact)

    assert raised.value.file == "manifest.json"


def test_a_model_trained_on_another_feature_schema_is_refused(tmp_path: Path) -> None:
    other = "f" * 64
    artifact = write_artifact(tmp_path, artifact_contents(schema=other))

    with capture_logs() as logs, pytest.raises(FraudModelError) as raised:
        load(artifact)

    assert raised.value.rule == "fraud_model.schema_mismatch"
    mismatch = next(log for log in logs if log["event"] == "fraud.schema_mismatch")
    assert mismatch["expected_schema_hash"] == schema_hash()
    assert mismatch["manifest_schema_hash"] == other
    assert logs[-1]["rule"] == "fraud_model.schema_mismatch"


def test_coefficients_in_another_feature_order_are_refused(tmp_path: Path) -> None:
    shuffled = (FEATURE_NAMES[1], FEATURE_NAMES[0], *FEATURE_NAMES[2:])
    artifact = write_artifact(tmp_path, artifact_contents(features=shuffled))

    with pytest.raises(FraudModelError) as raised:
        load(artifact)

    assert (raised.value.rule, raised.value.file) == (
        "fraud_model.feature_names_mismatch",
        "logistic.json",
    )


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("calibrator.json", b'{"method": "isotonic", "input": "raw_score", "a": 1, "b": 0}\n'),
        ("decision.json", b'{"review_cost_cents": 0}\n'),
        ("logistic.json", b"not json"),
    ],
)
def test_a_listed_file_with_invalid_content_is_refused_naming_it(
    tmp_path: Path, name: str, content: bytes
) -> None:
    contents = artifact_contents()
    manifest = json.loads(contents["manifest.json"])
    for entry in manifest["files"]:
        if entry["name"] == name:
            entry["size_bytes"], entry["sha256"] = len(content), hashlib.sha256(content).hexdigest()
    contents |= {name: content, "manifest.json": json.dumps(manifest).encode()}
    artifact = write_artifact(tmp_path, contents)

    with pytest.raises(FraudModelError) as raised:
        load(artifact)

    assert (raised.value.rule, raised.value.file) == ("fraud_model.file_invalid", name)


def test_impossible_coefficients_are_refused(tmp_path: Path) -> None:
    contents = artifact_contents()
    logistic = json.loads(contents["logistic.json"])
    logistic["scales"][0] = 0.0
    data = json.dumps(logistic).encode()
    manifest = json.loads(contents["manifest.json"])
    for entry in manifest["files"]:
        if entry["name"] == "logistic.json":
            entry["size_bytes"], entry["sha256"] = len(data), hashlib.sha256(data).hexdigest()
    contents |= {"logistic.json": data, "manifest.json": json.dumps(manifest).encode()}
    artifact = write_artifact(tmp_path, contents)

    with pytest.raises(FraudModelError) as raised:
        load(artifact)

    assert raised.value.rule == "fraud_model.file_invalid"


def test_fetch_downloads_the_manifest_then_the_files_it_lists(tmp_path: Path) -> None:
    contents = artifact_contents()
    source = tmp_path / "source"
    source.mkdir()
    pinned = write_artifact(source, contents)
    hub = Hub(contents)
    target = tmp_path / "model"
    target.mkdir()

    downloaded = fetch_fraud_model(target, TEST_REPOSITORY, pinned.manifest, hub)

    assert downloaded[0] == "manifest.json"
    assert set(downloaded) == set(contents)
    load_fraud_model(target, TEST_REPOSITORY, pinned.manifest)


def test_a_second_fetch_downloads_nothing(tmp_path: Path) -> None:
    artifact = write_artifact(tmp_path)
    hub = Hub(artifact.contents)

    downloaded = fetch_fraud_model(tmp_path, TEST_REPOSITORY, artifact.manifest, hub)

    assert downloaded == ()
    assert hub.downloaded == []


def test_fetch_refuses_a_manifest_that_is_not_json(tmp_path: Path) -> None:
    bad = b"<html>not a manifest</html>"
    pinned = ModelFile("manifest.json", len(bad), hashlib.sha256(bad).hexdigest())

    with pytest.raises(FraudModelError) as raised:
        fetch_fraud_model(tmp_path, TEST_REPOSITORY, pinned, Hub({"manifest.json": bad}))

    assert (raised.value.rule, raised.value.file) == ("fraud_model.file_invalid", "manifest.json")
