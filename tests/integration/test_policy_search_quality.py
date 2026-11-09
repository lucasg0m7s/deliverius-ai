"""The quality gate of the hybrid search: it fails when recall@3 or MRR fall below the floor.

The floors are the measured value minus a margin of 0.05, rounded, from `docs/busca-politicas.md`.
It also guards the refusal of out-of-scope questions with the default rule and threshold.
"""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from support.database import settings_for
from support.retrieval_database import search_for

from deliverius.db.engine import create_engine
from deliverius.retrieval import (
    E5Embedder,
    PolicySearch,
    SearchMode,
    evaluate_mode,
    load_out_of_scope,
    load_pairs,
)
from deliverius.retrieval.evaluation import evaluate_confidence

pytestmark = pytest.mark.integration

# Measured on 2026-11-08 (docs/busca-politicas.md): hybrid, by section,
# recall@3 0.550 and MRR@3 0.414.
# The floor is the measured value minus 0.05: a drop beyond noise fails the build.
HYBRID_RECALL_AT_3_FLOOR = 0.50
HYBRID_MRR_FLOOR = 0.36
# Measured: rule similarity_only at 0.85 refuses 63.3% of the out-of-scope set
# and keeps 98.3% of the
# pairs reliable.
OUT_OF_SCOPE_REFUSAL_FLOOR = 0.58
PAIRS_RELIABLE_FLOOR = 0.95
EVAL = Path(__file__).resolve().parents[2] / "data" / "eval"
PAIRS = EVAL / "policy_qa.jsonl"
OUT_OF_SCOPE = EVAL / "out_of_scope.jsonl"


@pytest.fixture
async def search(indexed_policies: str, embedder: E5Embedder) -> AsyncIterator[PolicySearch]:
    engine = create_engine(settings_for(indexed_policies))
    try:
        yield search_for(engine, embedder)
    finally:
        await engine.dispose()


async def test_the_hybrid_search_stays_above_the_quality_floor(search: PolicySearch) -> None:
    report = await evaluate_mode(search, load_pairs(PAIRS), SearchMode.HYBRID)

    section = report.by_section
    assert section.recall_at_3 >= HYBRID_RECALL_AT_3_FLOOR, (
        f"recall@3 of the hybrid search is {section.recall_at_3:.3f}, "
        f"expected at least {HYBRID_RECALL_AT_3_FLOOR:.3f}"
    )
    assert section.mrr >= HYBRID_MRR_FLOOR, (
        f"MRR@3 of the hybrid search is {section.mrr:.3f}, expected at least {HYBRID_MRR_FLOOR:.3f}"
    )


async def test_the_default_rule_refuses_out_of_scope_questions_and_keeps_the_pairs(
    search: PolicySearch,
) -> None:
    report = await evaluate_confidence(search, load_pairs(PAIRS), load_out_of_scope(OUT_OF_SCOPE))

    refused = report.out_of_scope_unreliable_rate
    reliable = 1 - report.pairs_unreliable_rate
    assert refused >= OUT_OF_SCOPE_REFUSAL_FLOOR, (
        f"{refused:.3f} of the out-of-scope questions refused, expected at least "
        f"{OUT_OF_SCOPE_REFUSAL_FLOOR:.3f}"
    )
    assert reliable >= PAIRS_RELIABLE_FLOOR, (
        f"{reliable:.3f} of the pairs reliable, expected at least {PAIRS_RELIABLE_FLOOR:.3f}"
    )
