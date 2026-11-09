"""The CI quality gate: the published model, scored by the application code, on the pinned test set.

`python -m research.fraud.gate` downloads what is missing (the dataset at its pinned commit, the
model at its pinned revision), rebuilds the October features with the current code and scores
them with the application's scorer. A feature change that breaks the served model fails here:
PR-AUC below the floor or Brier above the ceiling, both shown with the measured value.
"""

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import structlog

from deliverius.artifacts import fetch_files
from deliverius.cli.console import use_utf8_output
from deliverius.config import Settings
from deliverius.fraud import FraudScorer, LogisticFraudScorer, fetch_fraud_model, load_fraud_model
from deliverius.fraud.models import ClaimContext
from research.fraud.config import GateSection, load_config
from research.fraud.dataset import ClaimTable, load_claim_table
from research.fraud.evaluate import brier, pr_auc
from research.fraud.split import split_by_month


@dataclass(frozen=True, slots=True)
class GateResult:
    pr_auc: float
    brier: float
    min_pr_auc: float
    max_brier: float

    @property
    def passed(self) -> bool:
        return self.pr_auc >= self.min_pr_auc and self.brier <= self.max_brier

    def lines(self) -> list[str]:
        def mark(ok: bool) -> str:
            return "ok" if ok else "FALHOU"

        return [
            f"PR-AUC {self.pr_auc:.4f} (piso {self.min_pr_auc:.4f}): "
            f"{mark(self.pr_auc >= self.min_pr_auc)}",
            f"Brier  {self.brier:.4f} (teto {self.max_brier:.4f}): "
            f"{mark(self.brier <= self.max_brier)}",
        ]


class GateNotConfiguredError(Exception):
    """The floor and ceiling are filled from the single test run; without them there is no gate."""


def gate_limits(gate: GateSection) -> tuple[float, float]:
    if gate.min_pr_auc is None or gate.max_brier is None:
        raise GateNotConfiguredError
    return gate.min_pr_auc, gate.max_brier


def check(test: ClaimTable, scorer: FraudScorer, gate: GateSection) -> GateResult:
    min_pr_auc, max_brier = gate_limits(gate)
    probability = np.array(asyncio.run(_score_all(scorer, test.contexts)))
    return GateResult(
        pr_auc=pr_auc(test.is_fraud, probability),
        brier=brier(test.is_fraud, probability),
        min_pr_auc=min_pr_auc,
        max_brier=max_brier,
    )


async def _score_all(scorer: FraudScorer, contexts: Sequence[ClaimContext]) -> list[float]:
    return [(await scorer.score(context)).probability for context in contexts]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m research.fraud.gate")
    parser.add_argument("--model-dir", type=Path, default=None, help="padrão: FRAUD_MODEL_DIR")
    args = parser.parse_args(argv)
    use_utf8_output()
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING))
    config = load_config()
    model_dir: Path = args.model_dir or Settings().fraud_model_dir
    dataset_dir = config.dataset.directory
    dataset_dir.mkdir(parents=True, exist_ok=True)
    fetch_files(config.dataset.pinned, dataset_dir, config.dataset.model_files)
    model_dir.mkdir(parents=True, exist_ok=True)
    fetch_fraud_model(model_dir)
    scorer = LogisticFraudScorer(load_fraud_model(model_dir))
    table = load_claim_table(dataset_dir)
    test = table.subset(split_by_month(table.claimed_at, config.split).test)
    result = check(test, scorer, config.gate)
    sys.stdout.write(f"Porta do modelo {scorer.model_version} em {len(test)} reclamações:\n")
    for line in result.lines():
        sys.stdout.write(f"  {line}\n")
    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main())
