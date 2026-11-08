"""Writing and reading a generation: deterministic JSONL, an atomic folder, a manifest that vouches.

The bytes of a file depend only on its records. The folder appears all at once, with the manifest
written last, so a crash leaves a temporary folder and never a dataset that looks complete.
"""

import hashlib
import json
import shutil
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path, PurePosixPath

import structlog
from pydantic import BaseModel

from deliverius.datagen.records import Manifest
from deliverius.shared import DeliveriusError

MANIFEST_FILE = "manifest.json"

logger = structlog.get_logger(__name__)


class DatasetError(DeliveriusError):
    """The dataset folder cannot be written or trusted; `file` names the culprit, if any."""

    def __init__(self, rule: str, file: str | None = None) -> None:
        super().__init__(rule if file is None else f"{rule}: {file}")
        self.rule = rule
        self.file = file


def render_jsonl(records: Iterable[BaseModel]) -> bytes:
    lines = (
        json.dumps(
            record.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        for record in records
    )
    return "".join(f"{line}\n" for line in lines).encode("utf-8")


def sha256_hex(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def write_dataset(
    destination: Path,
    files: Mapping[str, Sequence[BaseModel]],
    manifest: Manifest,
    *,
    overwrite: bool = False,
) -> Manifest:
    """Write `files` and then the manifest, which gets the SHA-256 of every file."""
    rendered = {_checked_name(name): render_jsonl(records) for name, records in files.items()}
    if destination.exists() and not overwrite:
        raise DatasetError("dataset.exists", destination.name)
    final = manifest.model_copy(
        update={"files": {name: sha256_hex(content) for name, content in rendered.items()}}
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
    )
    try:
        for name, content in rendered.items():
            target = staging / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        _write_manifest(staging, final)
        _move_into_place(staging, destination, overwrite=overwrite)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    logger.info("datagen.dataset_written", files=len(rendered), version=final.datagen_version)
    return final


def read_manifest(directory: Path) -> Manifest:
    path = directory / MANIFEST_FILE
    if not path.is_file():
        raise DatasetError("dataset.manifest_missing", directory.name)
    try:
        return Manifest.model_validate_json(path.read_bytes())
    except ValueError as error:
        raise DatasetError("dataset.manifest_invalid", MANIFEST_FILE) from error


def read_records[T: BaseModel](directory: Path, name: str, model: type[T]) -> list[T]:
    """The rows of one file, after checking its hash against the manifest."""
    content = _read_verified(directory, read_manifest(directory), name)
    return [model.model_validate_json(line) for line in content.splitlines() if line]


def verify_dataset(directory: Path) -> Manifest:
    """Check every file the manifest lists; the first one that does not match is named."""
    manifest = read_manifest(directory)
    for name in manifest.files:
        _read_verified(directory, manifest, name)
    return manifest


def _read_verified(directory: Path, manifest: Manifest, name: str) -> bytes:
    expected = manifest.files.get(name)
    if expected is None:
        raise DatasetError("dataset.file_not_in_manifest", name)
    path = directory / _checked_name(name)
    if not path.is_file():
        raise DatasetError("dataset.file_missing", name)
    content = path.read_bytes()
    if sha256_hex(content) != expected:
        raise DatasetError("dataset.hash_mismatch", name)
    return content


def _checked_name(name: str) -> str:
    path = PurePosixPath(name)
    if not name or path.is_absolute() or ".." in path.parts or name == MANIFEST_FILE:
        raise DatasetError("dataset.invalid_file_name", name)
    return name


def _write_manifest(directory: Path, manifest: Manifest) -> None:
    text = json.dumps(
        manifest.model_dump(mode="json"), sort_keys=True, indent=2, ensure_ascii=False
    )
    (directory / MANIFEST_FILE).write_bytes(f"{text}\n".encode())


def _move_into_place(staging: Path, destination: Path, *, overwrite: bool) -> None:
    if not destination.exists():
        staging.rename(destination)
        return
    if not overwrite:
        raise DatasetError("dataset.exists", destination.name)
    previous = staging.with_name(f"{staging.name}.previous")
    destination.rename(previous)
    try:
        staging.rename(destination)
    except BaseException:
        previous.rename(destination)
        raise
    shutil.rmtree(previous, ignore_errors=True)
