"""Fine-tuning and exporting a tiny random BERT end to end. Needs the `finetune` group."""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from support.intent_data import learnable_set
from support.tiny_bert import save_tiny_bert

from research.intent.candidate import Approach, read_candidate
from research.intent.config import load_config
from research.intent.data import Corpus
from research.intent.export import export_candidate, load_bertimbau
from research.intent.finetune import RESULT_FILE, RunSettings, train_run

pytestmark = pytest.mark.finetune


@pytest.fixture(scope="session")
def tiny_base(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return save_tiny_bert(tmp_path_factory.mktemp("tiny-base"))


def tiny_settings(base: Path) -> RunSettings:
    section = load_config().finetune.model_copy(
        update={"base_model": str(base), "max_epochs": 2, "batch_size": 8}
    )
    # A local folder has no revision to pin.
    return replace(RunSettings.of(section, 5e-4, 1, 32), base_revision=None)


def test_a_run_writes_safe_weights_the_tokenizer_and_the_epochs(
    tiny_base: Path, tmp_path: Path
) -> None:
    settings = tiny_settings(tiny_base)

    result = train_run(
        settings, learnable_set(Corpus.TRAIN, 4), learnable_set(Corpus.VALIDATION, 2), tmp_path
    )

    names = {path.name for path in tmp_path.iterdir()}
    assert {"model.safetensors", "config.json", "tokenizer.json", "epochs.json"} <= names
    assert not any(name.endswith((".bin", ".pt", ".pkl")) for name in names)
    assert json.loads((tmp_path / RESULT_FILE).read_text(encoding="utf-8")) == result
    assert 0 <= result["macro_f1"] <= 1
    assert result["max_length"] == 32
    config = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert config["id2label"]["9"] == "sensitive"


@pytest.fixture(scope="session")
def tiny_run(tiny_base: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    folder = tmp_path_factory.mktemp("tiny-run")
    train_run(
        tiny_settings(tiny_base),
        learnable_set(Corpus.TRAIN, 4),
        learnable_set(Corpus.VALIDATION, 2),
        folder,
    )
    return folder


def test_the_export_freezes_an_onnx_candidate_that_predicts_without_torch(
    tiny_run: Path, tmp_path: Path
) -> None:
    validation = learnable_set(Corpus.VALIDATION, 3)

    document = export_candidate(tiny_run, validation, load_config(), "run-1", tmp_path)

    folder = tmp_path / "bertimbau"
    assert read_candidate(folder) == document
    assert document.approach is Approach.BERTIMBAU
    assert set(document.files) == {"model.onnx", "tokenizer.json"}
    assert document.validation["max_abs_logit_difference"] <= 1e-4
    assert document.validation["kept_graph"] in {"int8", "fp32"}
    assert document.temperature is not None
    logits = load_bertimbau(folder, 32, threads=1).logits(validation.texts)
    assert logits.shape == (len(validation), 11)
