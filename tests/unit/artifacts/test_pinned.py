import hashlib
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from deliverius.artifacts import (
    ArtifactDownloadError,
    ArtifactFileError,
    ModelFile,
    PinnedRepository,
    fetch_files,
    verify_files,
)
from deliverius.shared.errors import ExternalServiceError

REPOSITORY = PinnedRepository("someone/some-model", "a" * 40)

CONTENT = {
    "onnx/model.onnx": b"pretend this is a network" * 100,
    "onnx/tokenizer.json": b'{"vocab": []}',
    "onnx/config.json": b'{"hidden_size": 384}',
}
EXPECTED = tuple(
    ModelFile(name, len(data), hashlib.sha256(data).hexdigest()) for name, data in CONTENT.items()
)


def write_all(model_dir: Path) -> None:
    for name, data in CONTENT.items():
        path = model_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


class RecordingDownloader:
    def __init__(self, content: dict[str, bytes] = CONTENT) -> None:
        self.content = content
        self.downloaded: list[str] = []

    def __call__(self, file: ModelFile, model_dir: Path) -> None:
        self.downloaded.append(file.name)
        path = model_dir / file.name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.content[file.name])


def test_matching_files_are_accepted(tmp_path: Path) -> None:
    write_all(tmp_path)

    with capture_logs() as logs:
        verify_files(REPOSITORY, tmp_path, EXPECTED)

    assert [log["event"] for log in logs] == ["model_files.verified"]
    assert logs[0]["repository"] == "someone/some-model"


def test_a_missing_file_is_refused_naming_it(tmp_path: Path) -> None:
    write_all(tmp_path)
    (tmp_path / "onnx/tokenizer.json").unlink()

    with pytest.raises(ArtifactFileError, match=r"onnx/tokenizer\.json") as raised:
        verify_files(REPOSITORY, tmp_path, EXPECTED)

    assert raised.value.rule == "model_file.missing"
    assert raised.value.file == "onnx/tokenizer.json"


def test_a_file_of_another_size_is_refused_naming_it(tmp_path: Path) -> None:
    write_all(tmp_path)
    (tmp_path / "onnx/model.onnx").write_bytes(CONTENT["onnx/model.onnx"][:-1])

    with pytest.raises(ArtifactFileError, match=r"onnx/model\.onnx") as raised:
        verify_files(REPOSITORY, tmp_path, EXPECTED)

    assert raised.value.rule == "model_file.size_mismatch"


def test_a_tampered_file_of_the_same_size_is_refused_naming_it(tmp_path: Path) -> None:
    write_all(tmp_path)
    original = CONTENT["onnx/model.onnx"]
    (tmp_path / "onnx/model.onnx").write_bytes(b"X" + original[1:])

    with (
        capture_logs() as logs,
        pytest.raises(ArtifactFileError, match=r"onnx/model\.onnx") as raised,
    ):
        verify_files(REPOSITORY, tmp_path, EXPECTED)

    assert raised.value.rule == "model_file.hash_mismatch"
    assert logs[0]["event"] == "model_files.rejected"
    assert logs[0]["file"] == "onnx/model.onnx"
    assert logs[0]["repository"] == "someone/some-model"


def test_a_directory_in_place_of_a_file_is_refused(tmp_path: Path) -> None:
    write_all(tmp_path)
    (tmp_path / "onnx/config.json").unlink()
    (tmp_path / "onnx/config.json").mkdir()

    with pytest.raises(ArtifactFileError, match=r"onnx/config\.json"):
        verify_files(REPOSITORY, tmp_path, EXPECTED)


def test_fetch_downloads_every_missing_file(tmp_path: Path) -> None:
    downloader = RecordingDownloader()

    downloaded = fetch_files(REPOSITORY, tmp_path, EXPECTED, downloader)

    assert set(downloaded) == set(CONTENT)
    verify_files(REPOSITORY, tmp_path, EXPECTED)


def test_fetch_skips_files_that_already_match(tmp_path: Path) -> None:
    write_all(tmp_path)
    downloader = RecordingDownloader()

    downloaded = fetch_files(REPOSITORY, tmp_path, EXPECTED, downloader)

    assert downloaded == ()
    assert downloader.downloaded == []


def test_fetch_downloads_again_only_the_file_that_is_wrong(tmp_path: Path) -> None:
    write_all(tmp_path)
    (tmp_path / "onnx/tokenizer.json").write_bytes(b"truncated")
    downloader = RecordingDownloader()

    downloaded = fetch_files(REPOSITORY, tmp_path, EXPECTED, downloader)

    assert downloaded == ("onnx/tokenizer.json",)
    verify_files(REPOSITORY, tmp_path, EXPECTED)


def test_fetch_refuses_a_download_that_does_not_match(tmp_path: Path) -> None:
    downloader = RecordingDownloader({**CONTENT, "onnx/config.json": b"{}"})

    with pytest.raises(ArtifactFileError, match=r"onnx/config\.json") as raised:
        fetch_files(REPOSITORY, tmp_path, EXPECTED, downloader)

    assert raised.value.rule == "model_file.size_mismatch"


def test_fetch_removes_a_wrong_file_before_downloading(tmp_path: Path) -> None:
    write_all(tmp_path)
    (tmp_path / "onnx/config.json").write_bytes(b"partial")
    seen_at_download: list[bool] = []

    def download(file: ModelFile, model_dir: Path) -> None:
        seen_at_download.append((model_dir / file.name).exists())
        (model_dir / file.name).write_bytes(CONTENT[file.name])

    fetch_files(REPOSITORY, tmp_path, EXPECTED, download)

    assert seen_at_download == [False]


@pytest.mark.parametrize("repo_type", ["model", "dataset"])
def test_the_default_downloader_asks_the_hub_for_the_pinned_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repo_type: str
) -> None:
    calls: list[tuple[tuple[str, str], dict[str, object]]] = []

    def fake_hub_download(*args: str, **kwargs: object) -> str:
        calls.append(((args[0], args[1]), kwargs))
        return ""

    monkeypatch.setattr("deliverius.artifacts.pinned.hf_hub_download", fake_hub_download)
    repository = PinnedRepository("someone/data", "b" * 40, repo_type)  # type: ignore[arg-type]

    with pytest.raises(ArtifactFileError):
        fetch_files(repository, tmp_path, (EXPECTED[2],))

    assert calls == [
        (
            ("someone/data", "onnx/config.json"),
            {"revision": "b" * 40, "repo_type": repo_type, "local_dir": tmp_path},
        )
    ]


@pytest.mark.parametrize(
    "failure", [ConnectionError("offline"), TimeoutError(), RuntimeError("503")]
)
def test_a_hub_failure_becomes_a_download_error_naming_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    def failing_hub_download(*args: str, **kwargs: object) -> str:
        raise failure

    monkeypatch.setattr("deliverius.artifacts.pinned.hf_hub_download", failing_hub_download)

    with capture_logs() as logs, pytest.raises(ArtifactDownloadError) as error:
        fetch_files(REPOSITORY, tmp_path, (EXPECTED[2],))

    assert error.value.file == "onnx/config.json"
    assert isinstance(error.value, ExternalServiceError)
    assert error.value.__cause__ is failure
    assert logs[0]["event"] == "model_files.download_failed"
    assert logs[0]["repository"] == "someone/some-model"
    assert not (tmp_path / "onnx/config.json").exists()


def test_a_download_that_dies_halfway_leaves_no_file_that_passes(tmp_path: Path) -> None:
    def dies_halfway(file: ModelFile, directory: Path) -> None:
        path = directory / file.name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(CONTENT[file.name][:3])
        raise ArtifactDownloadError(file.name)

    with pytest.raises(ArtifactDownloadError):
        fetch_files(REPOSITORY, tmp_path, EXPECTED, dies_halfway)

    with pytest.raises(ArtifactFileError):
        verify_files(REPOSITORY, tmp_path, EXPECTED)
    downloaded = fetch_files(REPOSITORY, tmp_path, EXPECTED, RecordingDownloader())
    assert set(downloaded) == set(CONTENT)
