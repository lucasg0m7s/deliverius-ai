"""The embedding model files: a pinned revision, and a check of every file before it is used.

The files come from the official `intfloat/multilingual-e5-small` repository (MIT) at one commit.
Size and SHA-256 of each file live here, so a swapped, truncated or tampered file is refused before
`onnxruntime` or `tokenizers` read it. The download is an explicit command (`fetch-model`): the
application itself never reaches the network for the model.
"""

from collections.abc import Iterable
from pathlib import Path
from typing import Final

from deliverius.artifacts import (
    ArtifactDownloadError,
    ArtifactFileError,
    Downloader,
    ModelFile,
    PinnedRepository,
    fetch_files,
    verify_files,
)

MODEL_REPOSITORY: Final = "intfloat/multilingual-e5-small"
MODEL_REVISION: Final = "614241f622f53c4eeff9890bdc4f31cfecc418b3"
REPOSITORY: Final = PinnedRepository(MODEL_REPOSITORY, MODEL_REVISION)

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

# The names the search has always exposed; the behaviour lives in `deliverius.artifacts`.
ModelFileError = ArtifactFileError
ModelDownloadError = ArtifactDownloadError

__all__ = [
    "MODEL_CONFIG",
    "MODEL_FILES",
    "MODEL_REPOSITORY",
    "MODEL_REVISION",
    "ONNX_MODEL",
    "REPOSITORY",
    "TOKENIZER",
    "Downloader",
    "ModelDownloadError",
    "ModelFile",
    "ModelFileError",
    "fetch_model",
    "verify_model_files",
]


def verify_model_files(model_dir: Path, expected: Iterable[ModelFile] = MODEL_FILES) -> None:
    """Raise `ModelFileError` for the first file that is absent, of another size or another hash."""
    verify_files(REPOSITORY, model_dir, expected)


def fetch_model(
    model_dir: Path,
    expected: Iterable[ModelFile] = MODEL_FILES,
    download: Downloader | None = None,
) -> tuple[str, ...]:
    """Download the files that are missing or wrong, verifying each; returns what was downloaded."""
    return fetch_files(REPOSITORY, model_dir, expected, download)
