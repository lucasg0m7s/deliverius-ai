"""The dataset pin is shared: one file, read by the fraud and the intent research."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from research.dataset import DATASET_PIN_PATH, DatasetPin, fetch_dataset, load_dataset_pin
from research.fraud.config import load_config


def test_the_pin_names_every_file_with_its_size_and_hash() -> None:
    pin = load_dataset_pin()

    names = {file.name for file in pin.files}
    assert {"messages/train.jsonl", "messages_holdout/test.jsonl", "manifest.json"} <= names
    assert pin.pinned.revision == pin.revision
    assert pin.pinned.repo_type == "dataset"
    assert len(pin.model_files) == len(pin.files)


def test_the_fraud_research_reads_the_shared_pin() -> None:
    assert load_config().dataset == load_dataset_pin()


def test_the_fraud_config_no_longer_holds_its_own_pin() -> None:
    fraud_yaml = (DATASET_PIN_PATH.parent / "fraud" / "config.yaml").read_text(encoding="utf-8")

    assert "revision:" not in fraud_yaml


def test_a_pin_with_a_short_revision_is_refused(tmp_path: Path) -> None:
    text = DATASET_PIN_PATH.read_text(encoding="utf-8")
    pin = load_dataset_pin()
    broken = tmp_path / "dataset.yaml"
    broken.write_text(text.replace(pin.revision, pin.revision[:8]), encoding="utf-8")

    with pytest.raises(ValidationError):
        load_dataset_pin(broken)


def test_fetch_creates_the_folder_and_downloads_only_what_is_pinned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, int]] = []

    def fake_fetch(repository: object, directory: Path, files: tuple[object, ...]) -> None:
        calls.append((str(directory), len(files)))

    monkeypatch.setattr("research.dataset.fetch_files", fake_fetch)
    pin = DatasetPin.model_validate(
        load_dataset_pin().model_dump() | {"directory": tmp_path / "a" / "b"}
    )

    folder = fetch_dataset(pin)

    assert folder.is_dir()
    assert calls == [(str(tmp_path / "a" / "b"), len(pin.files))]
