"""The pinned synthetic dataset (`dataset.yaml`): what every study reads, file by file.

The revision is the commit of the version tag on the Hub, and each file has its size and SHA-256,
so a download or a cached copy is the exact bytes the studies were written against.
"""

from pathlib import Path
from typing import Final

import yaml
from pydantic import BaseModel, ConfigDict, Field

from deliverius.artifacts import ModelFile, PinnedRepository, fetch_files

DATASET_PIN_PATH: Final = Path(__file__).with_name("dataset.yaml")


class _Pin(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class PinnedFile(_Pin):
    name: str
    size_bytes: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    def to_model_file(self) -> ModelFile:
        return ModelFile(self.name, self.size_bytes, self.sha256)


class DatasetPin(_Pin):
    directory: Path
    repository: str
    version: str
    revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    files: tuple[PinnedFile, ...]

    @property
    def pinned(self) -> PinnedRepository:
        return PinnedRepository(self.repository, self.revision, "dataset")

    @property
    def model_files(self) -> tuple[ModelFile, ...]:
        return tuple(file.to_model_file() for file in self.files)


def load_dataset_pin(path: Path = DATASET_PIN_PATH) -> DatasetPin:
    return DatasetPin.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def fetch_dataset(pin: DatasetPin) -> Path:
    """Downloads the files missing from `pin.directory`, each checked against its SHA-256."""
    pin.directory.mkdir(parents=True, exist_ok=True)
    fetch_files(pin.pinned, pin.directory, pin.model_files)
    return pin.directory
