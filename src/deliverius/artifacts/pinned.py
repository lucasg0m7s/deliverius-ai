"""Files of a Hugging Face repository at one pinned commit, each checked before it is used.

Size and SHA-256 of every file live in the caller's code, so a swapped, truncated or tampered file
is refused before anything reads it. The download is an explicit command: the application never
reaches the network for a model.
"""

import hashlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

import structlog
from huggingface_hub import hf_hub_download

from deliverius.shared.errors import DeliveriusError, ExternalServiceError

logger = structlog.get_logger(__name__)

_HASH_CHUNK_BYTES: Final = 1024 * 1024

type RepositoryType = Literal["model", "dataset"]


@dataclass(frozen=True, slots=True)
class PinnedRepository:
    """A Hugging Face repository at one commit (`revision` is the full hash, never a branch)."""

    repo_id: str
    revision: str
    repo_type: RepositoryType = "model"


@dataclass(frozen=True, slots=True)
class ModelFile:
    """A file of the repository, by path, with the size and SHA-256 it must have."""

    name: str
    size_bytes: int
    sha256: str


type Downloader = Callable[[ModelFile, Path], None]


class ArtifactFileError(DeliveriusError):
    """A file is missing or differs from the pinned one; `file` names it."""

    def __init__(self, rule: str, file: str) -> None:
        super().__init__(f"{rule}: {file}")
        self.rule = rule
        self.file = file


class ArtifactDownloadError(ExternalServiceError):
    """The hub did not deliver `file` (network down, timeout, refused): run the command again."""

    def __init__(self, file: str) -> None:
        super().__init__(f"model_download.failed: {file}")
        self.file = file


def verify_files(
    repository: PinnedRepository, directory: Path, expected: Iterable[ModelFile]
) -> None:
    """Raise `ArtifactFileError` for the first file that is absent, of another size or hash."""
    for file in expected:
        _require_match(repository, directory, file)
    logger.info("model_files.verified", repository=repository.repo_id, directory=str(directory))


def fetch_files(
    repository: PinnedRepository,
    directory: Path,
    expected: Iterable[ModelFile],
    download: Downloader | None = None,
) -> tuple[str, ...]:
    """Download the files that are missing or wrong, verifying each; returns what was downloaded.

    A file that already matches is not downloaded again, nor hashed a second time. A partial or
    altered file is removed first, so it can never be mistaken for a finished download.
    """
    download = download or hub_downloader(repository)
    downloaded: list[str] = []
    for file in expected:
        if _mismatch(directory, file) is None:
            continue
        (directory / file.name).unlink(missing_ok=True)
        download(file, directory)
        downloaded.append(file.name)
        _require_match(repository, directory, file)
    logger.info("model_files.verified", repository=repository.repo_id, directory=str(directory))
    return tuple(downloaded)


def hub_downloader(repository: PinnedRepository) -> Downloader:
    """Downloads one file of `repository` at its pinned revision into a local folder."""

    def download(file: ModelFile, directory: Path) -> None:
        try:
            hf_hub_download(
                repository.repo_id,
                file.name,
                revision=repository.revision,
                repo_type=repository.repo_type,
                local_dir=directory,
            )
        except Exception as error:
            # The hub client has its own HTTP error hierarchy, not all of it an `OSError`.
            logger.warning(
                "model_files.download_failed", repository=repository.repo_id, file=file.name
            )
            raise ArtifactDownloadError(file.name) from error

    return download


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _require_match(repository: PinnedRepository, directory: Path, file: ModelFile) -> None:
    rule = _mismatch(directory, file)
    if rule is not None:
        logger.warning(
            "model_files.rejected", repository=repository.repo_id, file=file.name, rule=rule
        )
        raise ArtifactFileError(rule, file.name)


def _mismatch(directory: Path, file: ModelFile) -> str | None:
    path = directory / file.name
    if not path.is_file():
        return "model_file.missing"
    if path.stat().st_size != file.size_bytes:
        return "model_file.size_mismatch"
    if sha256_of(path) != file.sha256:
        return "model_file.hash_mismatch"
    return None
