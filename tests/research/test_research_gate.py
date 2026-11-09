"""The CI gate fails a bad model and shows the measured value next to the limit."""

import numpy as np
import pytest
from support.fraud_builders import claim_ctx, fraud_model, logistic_model

from deliverius.fraud import FEATURE_NAMES, FixedFraudScorer, LogisticFraudScorer
from research.fraud.config import GateSection
from research.fraud.dataset import ClaimTable, feature_matrix
from research.fraud.gate import GateNotConfiguredError, check, gate_limits

GATE = GateSection(pr_auc_margin=0.01, brier_margin=0.005, min_pr_auc=0.6, max_brier=0.1)


def table_of(labels: list[int]) -> ClaimTable:
    """Claims whose requested fraction tells fraud apart: the hand-made model ranks them right."""
    contexts = tuple(
        claim_ctx(total_cents=10_000, requested_cents=9_500 if fraud else 1_000) for fraud in labels
    )
    return ClaimTable(
        claim_ids=tuple(context.order_id for context in contexts),
        claimed_at=tuple(context.claimed_at for context in contexts),
        contexts=contexts,
        features=feature_matrix(contexts),
        is_fraud=np.array(labels, dtype=np.int64),
        requested_cents=np.array([c.requested.cents for c in contexts], dtype=np.int64),
    )


LABELS = [1, 0, 0, 0, 1, 0, 0, 0, 0, 1] * 3


def test_a_model_that_ranks_well_passes() -> None:
    weights = tuple(20.0 if name == "requested_fraction" else 0.0 for name in FEATURE_NAMES)
    model = fraud_model(logistic_model(coefficients=weights), a=-1.0, b=0.0)

    result = check(table_of(LABELS), LogisticFraudScorer(model), GATE)

    assert result.pr_auc == 1.0
    assert result.passed, result.lines()


def test_a_bad_model_fails_and_shows_value_and_limit() -> None:
    result = check(table_of(LABELS), FixedFraudScorer(0.5), GATE)

    assert not result.passed
    assert result.pr_auc == pytest.approx(0.3)
    assert result.brier == pytest.approx(0.25)
    assert result.lines() == [
        "PR-AUC 0.3000 (piso 0.6000): FALHOU",
        "Brier  0.2500 (teto 0.1000): FALHOU",
    ]


def test_without_measured_limits_there_is_no_gate() -> None:
    with pytest.raises(GateNotConfiguredError):
        gate_limits(GATE.model_copy(update={"min_pr_auc": None}))
