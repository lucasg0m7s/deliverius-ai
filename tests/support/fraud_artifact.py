"""A fraud model folder written by the test, with a manifest that vouches for each file."""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from deliverius.artifacts import ModelFile, PinnedRepository
from deliverius.fraud.features import FEATURE_NAMES, FEATURES_VERSION, schema_hash

TEST_REPOSITORY = PinnedRepository("someone/fraud-model", "c" * 40)
SIZE = len(FEATURE_NAMES)


@dataclass(frozen=True)
class WrittenArtifact:
    directory: Path
    manifest: ModelFile
    contents: dict[str, bytes]


def _json(document: dict[str, Any]) -> bytes:
    return f"{json.dumps(document, sort_keys=True, indent=2)}\n".encode()


def artifact_contents(
    *, schema: str | None = None, features: tuple[str, ...] = FEATURE_NAMES
) -> dict[str, bytes]:
    files = {
        "logistic.json": _json(
            {
                "features": list(features),
                "medians": [1.0] * SIZE,
                "means": [0.5] * SIZE,
                "scales": [2.0] * SIZE,
                "coefficients": [0.1] * SIZE,
                "intercept": -2.0,
            }
        ),
        "calibrator.json": _json({"method": "sigmoid", "input": "raw_score", "a": -1.1, "b": 0.3}),
        "decision.json": _json({"review_cost_cents": 400}),
        "README.md": b"# Fraud model\n",
    }
    manifest = {
        "model_version": "1.0.0",
        "model_kind": "logistic_regression",
        "features_version": FEATURES_VERSION,
        "feature_schema_hash": schema or schema_hash(),
        "dataset": {"repository": "someone/data", "version": "1.0.0", "revision": "d" * 40},
        "mlflow_run_id": "run-1",
        "code_commit": "e" * 40,
        "metrics": {"test": {"pr_auc": 0.4, "brier": 0.09}},
        "files": [
            {"name": name, "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            for name, data in sorted(files.items())
        ],
    }
    return {**files, "manifest.json": _json(manifest)}


def write_artifact(directory: Path, contents: dict[str, bytes] | None = None) -> WrittenArtifact:
    contents = contents if contents is not None else artifact_contents()
    for name, data in contents.items():
        (directory / name).write_bytes(data)
    manifest = contents["manifest.json"]
    pinned = ModelFile("manifest.json", len(manifest), hashlib.sha256(manifest).hexdigest())
    return WrittenArtifact(directory, pinned, contents)
