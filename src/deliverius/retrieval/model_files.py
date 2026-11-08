"""The embedding model files: a pinned revision, and a check of every file before it is used.

The files come from the official `intfloat/multilingual-e5-small` repository (MIT) at one commit.
Size and SHA-256 of each file live here, so a swapped, truncated or tampered file is refused before
`onnxruntime` or `tokenizers` read it. The download is an explicit command (`fetch-model`): the
application itself never reaches the network for the model.
"""

import hashlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import structlog
from huggingface_hub import hf_hub_download

from deliverius.shared.errors import DeliveriusError

logger = structlog.get_logger(__name__)

MODEL_REPOSITORY: Final = "intfloat/multilingual-e5-small"
MODEL_REVISION: Final = "614241f622f53c4eeff9890bdc4f31cfecc418b3"
_HASH_CHUNK_BYTES: Final = 1024 * 1024


@dataclass(frozen=True, slots=True)
class ModelFile:
    """A file of the model repository, by path, with the size and SHA-256 it must have."""

    name: str
    size_bytes: int
    sha256: str


ONNX_MODEL: Final = ModelFile(
    "onnx/model.onnx",
    470_268_510,
    "ca456c06b3a9505ddfd9131408916dd79290368331e7d76bb621f1cba6bc8665",
)
TOKENIZER: Final = ModelFile(
    "onnx/tokenizer.json",
    17_082_730,
    "0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39",
)
MODEL_CONFIG: Final = ModelFile(
    "onnx/config.json",
    653,
    "bbb7c1333fc4b3e27fbc9cd5d2070aabcc1d4dfb99917c3633e772f97545a6b6",
)
MODEL_FILES: Final = (ONNX_MODEL, TOKENIZER, MODEL_CONFIG)

type Downloader = Callable[[ModelFile, Path], None]


class ModelFileError(DeliveriusError):
    """A model file is missing or differs from the pinned one; `file` names it."""

    def __init__(self, rule: str, file: str) -> None:
        super().__init__(f"{rule}: {file}")
        self.rule = rule
        self.file = file


def verify_model_files(model_dir: Path, expected: Iterable[ModelFile] = MODEL_FILES) -> None:
    """Raise `ModelFileError` for the first file that is absent, of another size or another hash."""
    for file in expected:
        rule = _mismatch(model_dir, file)
        if rule is not None:
            logger.warning("retrieval.model_rejected", file=file.name, rule=rule)
            raise ModelFileError(rule, file.name)
    logger.info("retrieval.model_verified", model_dir=str(model_dir))


def fetch_model(
    model_dir: Path,
    expected: Iterable[ModelFile] = MODEL_FILES,
    download: Downloader | None = None,
) -> tuple[str, ...]:
    """Download the files that are missing or wrong, then verify all; returns what was downloaded.

    A file that already matches is not downloaded again. A partial or altered file is removed first,
    so it can never be mistaken for a finished download.
    """
    files = tuple(expected)
    download = download or _download_from_hub
    downloaded: list[str] = []
    for file in files:
        if _mismatch(model_dir, file) is None:
            continue
        (model_dir / file.name).unlink(missing_ok=True)
        download(file, model_dir)
        downloaded.append(file.name)
    verify_model_files(model_dir, files)
    return tuple(downloaded)


def _download_from_hub(file: ModelFile, model_dir: Path) -> None:
    hf_hub_download(MODEL_REPOSITORY, file.name, revision=MODEL_REVISION, local_dir=model_dir)


def _mismatch(model_dir: Path, file: ModelFile) -> str | None:
    path = model_dir / file.name
    if not path.is_file():
        return "model_file.missing"
    if path.stat().st_size != file.size_bytes:
        return "model_file.size_mismatch"
    if _sha256(path) != file.sha256:
        return "model_file.hash_mismatch"
    return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()
