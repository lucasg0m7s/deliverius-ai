"""The embedding model keeps its pins and its public names; the checks live in `artifacts`."""

import hashlib
from pathlib import Path

import pytest

from deliverius.artifacts import ArtifactDownloadError, ArtifactFileError
from deliverius.retrieval import MODEL_FILES, MODEL_REVISION
from deliverius.retrieval.model_files import (
    MODEL_REPOSITORY,
    ModelDownloadError,
    ModelFile,
    ModelFileError,
    fetch_model,
    verify_model_files,
)

CONFIG = b'{"hidden_size": 384}'
ONLY_CONFIG = (ModelFile("onnx/config.json", len(CONFIG), hashlib.sha256(CONFIG).hexdigest()),)


def test_pinned_revision_is_a_full_commit_hash() -> None:
    assert len(MODEL_REVISION) == 40
    assert MODEL_REPOSITORY == "intfloat/multilingual-e5-small"


def test_every_pinned_file_has_a_sha256_and_a_size() -> None:
    assert {file.name for file in MODEL_FILES} == {
        "onnx/model.onnx",
        "onnx/tokenizer.json",
        "onnx/config.json",
    }
    for file in MODEL_FILES:
        assert len(file.sha256) == 64
        assert file.size_bytes > 0


def test_the_public_errors_are_the_artifact_errors() -> None:
    assert ModelFileError is ArtifactFileError
    assert ModelDownloadError is ArtifactDownloadError


def test_verify_refuses_a_missing_file_naming_it(tmp_path: Path) -> None:
    with pytest.raises(ModelFileError) as raised:
        verify_model_files(tmp_path, ONLY_CONFIG)

    assert raised.value.rule == "model_file.missing"
    assert raised.value.file == "onnx/config.json"


def test_fetch_asks_the_hub_for_the_e5_repository_at_the_pinned_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str, object]] = []

    def fake_hub_download(repo_id: str, name: str, **kwargs: object) -> str:
        calls.append((repo_id, name, kwargs["revision"]))
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_bytes(CONFIG)
        return ""

    monkeypatch.setattr("deliverius.artifacts.pinned.hf_hub_download", fake_hub_download)

    downloaded = fetch_model(tmp_path, ONLY_CONFIG)

    assert downloaded == ("onnx/config.json",)
    assert calls == [(MODEL_REPOSITORY, "onnx/config.json", MODEL_REVISION)]
