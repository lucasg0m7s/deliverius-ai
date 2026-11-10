"""The research configuration (`config.yaml` and the shared dataset pin), read once, validated."""

from pathlib import Path
from typing import Any, Final

import yaml
from pydantic import BaseModel, ConfigDict, Field

from research.dataset import DatasetPin, load_dataset_pin

CONFIG_PATH: Final = Path(__file__).with_name("config.yaml")


class _Section(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SplitSection(_Section):
    year: int
    train_months: tuple[int, ...] = Field(min_length=2)
    validation_month: int
    test_month: int


class LogisticSection(_Section):
    C: float = Field(gt=0)
    max_iter: int = Field(gt=0)


class LightGbmGrid(_Section):
    num_leaves: tuple[int, ...]
    min_child_samples: tuple[int, ...]
    learning_rate_and_estimators: tuple[tuple[float, int], ...]
    reg_lambda: tuple[float, ...]


class LightGbmSection(_Section):
    fixed: dict[str, Any]
    grid: LightGbmGrid


class GateSection(_Section):
    pr_auc_margin: float = Field(ge=0)
    brier_margin: float = Field(ge=0)
    min_pr_auc: float | None = None
    max_brier: float | None = None


class ModelSection(_Section):
    repository: str
    version: str


class ResearchConfig(_Section):
    seed: int
    dataset: DatasetPin
    split: SplitSection
    review_cost_cents: int = Field(gt=0)
    bootstrap_resamples: int = Field(gt=0)
    business_rule_claims_30d: int = Field(gt=0)
    logistic: LogisticSection
    lightgbm: LightGbmSection
    gate: GateSection
    model: ModelSection


def load_config(path: Path = CONFIG_PATH, dataset: DatasetPin | None = None) -> ResearchConfig:
    """`dataset` defaults to the shared pin in `research/dataset.yaml`."""
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    return ResearchConfig.model_validate(
        {**document, "dataset": dataset if dataset is not None else load_dataset_pin()}
    )
