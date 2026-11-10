"""The intent research configuration (`config.yaml` and the shared dataset pin), validated."""

from pathlib import Path
from typing import Final

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from research.dataset import DatasetPin, load_dataset_pin

CONFIG_PATH: Final = Path(__file__).with_name("config.yaml")


class _Section(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class HumanTestSection(_Section):
    path: Path
    min_per_intent: int = Field(gt=0)


class RuleSection(_Section):
    min_sensitive_recall: float = Field(gt=0, le=1)
    max_p95_ms: float = Field(gt=0)
    bootstrap_resamples: int = Field(gt=0)
    confidence_level: float = Field(gt=0, lt=1)


class CalibrationSection(_Section):
    ece_bins: int = Field(gt=0)
    temperature_bounds: tuple[float, float]

    @model_validator(mode="after")
    def _bounds_are_ordered(self) -> "CalibrationSection":
        low, high = self.temperature_bounds
        if not 0 < low < high:
            raise ValueError("calibration.temperature_bounds")
        return self


class RouterSection(_Section):
    max_error: float = Field(gt=0, lt=1)


class LatencySection(_Section):
    warmup_messages: int = Field(ge=0)
    intra_op_threads: int = Field(gt=0)


class TfidfSection(_Section):
    word_ngrams: tuple[int, int]
    char_ngrams: tuple[int, int]
    min_df: int = Field(gt=0)
    C_grid: tuple[float, ...] = Field(min_length=1)
    max_iter: int = Field(gt=0)


class E5Section(_Section):
    C_grid: tuple[float, ...] = Field(min_length=1)
    max_iter: int = Field(gt=0)


class LlmBaselineSection(_Section):
    prompt_version: int = Field(gt=0)
    examples_per_intent: int = Field(gt=0)
    sample_per_intent: int = Field(gt=0)
    cache_path: Path
    latency_path: Path


class FinetuneSection(_Section):
    base_model: str
    base_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    learning_rates: tuple[float, ...] = Field(min_length=1)
    search_seed: int
    seeds: tuple[int, ...] = Field(min_length=1)
    max_epochs: int = Field(gt=0)
    patience: int = Field(gt=0)
    batch_size: int = Field(gt=0)
    warmup_ratio: float = Field(ge=0, lt=1)
    weight_decay: float = Field(ge=0)
    short_max_length: int = Field(gt=0)
    long_max_length: int = Field(gt=0)
    long_share_threshold: float = Field(gt=0, lt=1)


class ExportSection(_Section):
    logits_tolerance: float = Field(gt=0)
    min_argmax_agreement: float = Field(gt=0, le=1)


class ModelSection(_Section):
    repository: str
    version: str


class IntentConfig(_Section):
    seed: int
    dataset: DatasetPin
    human_test: HumanTestSection
    rule: RuleSection
    calibration: CalibrationSection
    router: RouterSection
    latency: LatencySection
    tfidf: TfidfSection
    e5: E5Section
    llm_baseline: LlmBaselineSection
    finetune: FinetuneSection
    export: ExportSection
    model: ModelSection


def load_config(path: Path = CONFIG_PATH, dataset: DatasetPin | None = None) -> IntentConfig:
    """`dataset` defaults to the shared pin in `research/dataset.yaml`."""
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    return IntentConfig.model_validate(
        {**document, "dataset": dataset if dataset is not None else load_dataset_pin()}
    )
