"""A frozen candidate: the files that predict, and `candidate.json` with how they were chosen.

`write_candidate` writes into a temporary folder and renames it at the end, so a crash never
leaves a half-written candidate. Every file is numbers or text (JSON, ONNX, a tokenizer), never a
pickle, and `candidate.json` keeps its SHA-256: `read_candidate` refuses a file that changed.
"""

import hashlib
import json
import shutil
import tempfile
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

import structlog
from pydantic import BaseModel, ConfigDict, Field

from deliverius.shared.errors import DeliveriusError

logger = structlog.get_logger(__name__)

CANDIDATES_DIR: Final = Path("models/intent-candidate")
CANDIDATE_FILE: Final = "candidate.json"


class Approach(StrEnum):
    TFIDF = "tfidf"
    E5 = "e5"
    BERTIMBAU = "bertimbau"
    LLM = "llm"


class CandidateError(DeliveriusError):
    def __init__(self, rule: str, detail: str) -> None:
        super().__init__(f"{rule}: {detail}")
        self.rule = rule


class CandidateDocument(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    approach: Approach
    labels: tuple[str, ...]
    # `None` for the LLM, which states its own confidence.
    temperature: float | None = Field(default=None, gt=0)
    # `None` when no threshold keeps the validation error under the target.
    threshold: float | None
    params: dict[str, Any]
    seed: int
    code_commit: str
    code_dirty: bool
    dataset_version: str
    dataset_revision: str
    mlflow_run_id: str | None = None
    validation: dict[str, Any]
    files: dict[str, str] = Field(default_factory=dict)


def candidate_dir(approach: Approach, root: Path = CANDIDATES_DIR) -> Path:
    return root / approach.value


def canonical_json(document: BaseModel | Mapping[str, Any]) -> bytes:
    """Sorted keys, two-space indent, final newline: the same bytes for the same numbers."""
    data = document.model_dump(mode="json") if isinstance(document, BaseModel) else document
    return f"{json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False)}\n".encode()


def write_candidate(
    destination: Path, document: CandidateDocument, files: Mapping[str, bytes]
) -> CandidateDocument:
    """Writes `files` and the document (with their hashes) atomically; returns the document."""
    final = document.model_copy(
        update={"files": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}}
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent))
    try:
        for name, data in files.items():
            (staging / name).write_bytes(data)
        (staging / CANDIDATE_FILE).write_bytes(canonical_json(final))
        if destination.exists():
            shutil.rmtree(destination)
        staging.rename(destination)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    logger.info("intent_research.candidate_written", approach=final.approach)
    return final


def read_candidate(directory: Path) -> CandidateDocument:
    document = CandidateDocument.model_validate_json((directory / CANDIDATE_FILE).read_bytes())
    for name, expected in document.files.items():
        path = directory / name
        if not path.is_file():
            raise CandidateError("candidate.file_missing", name)
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise CandidateError("candidate.hash_mismatch", name)
    return document
