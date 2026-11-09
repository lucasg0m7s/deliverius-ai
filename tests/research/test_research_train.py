"""Training: determinism, the choice rule, and the exported numbers equal to scikit-learn."""

from pathlib import Path

import numpy as np
import pytest
from sklearn.calibration import CalibratedClassifierCV
from sklearn.frozen import FrozenEstimator
from support.research_data import synthetic_table

from deliverius.fraud.features import FEATURE_NAMES
from research.fraud.candidate import candidate_from, canonical_json, read_candidate, write_candidate
from research.fraud.config import ResearchConfig, load_config
from research.fraud.split import split_by_month
from research.fraud.train import (
    ProbabilityMismatchError,
    ResearchStop,
    assert_close,
    business_rule,
    fit_lightgbm,
    fit_logistic,
    lightgbm_grid,
    run_research,
    sigmoid_by_hand,
)

TABLE = synthetic_table()
PARAMS = {
    "num_leaves": 7,
    "min_child_samples": 20,
    "learning_rate": 0.1,
    "n_estimators": 30,
    "reg_lambda": 1.0,
}


def quick_config() -> ResearchConfig:
    config = load_config()
    grid = config.lightgbm.grid.model_copy(
        update={
            "num_leaves": (7,),
            "min_child_samples": (20,),
            "learning_rate_and_estimators": ((0.1, 30),),
            "reg_lambda": (1.0,),
        }
    )
    return config.model_copy(
        update={
            "bootstrap_resamples": 100,
            "lightgbm": config.lightgbm.model_copy(update={"grid": grid}),
        }
    )


def test_the_grid_has_about_a_dozen_points() -> None:
    points = list(lightgbm_grid(load_config().lightgbm))

    assert len(points) == 12
    assert len({tuple(sorted(p.items())) for p in points}) == 12


def test_the_same_seed_gives_the_same_lightgbm_and_logistic_probabilities() -> None:
    config = quick_config()
    x, y = TABLE.features[:300], TABLE.is_fraud[:300]

    first = fit_lightgbm(x, y, PARAMS, config).predict_proba(TABLE.features)[:, 1]
    second = fit_lightgbm(x, y, PARAMS, config).predict_proba(TABLE.features)[:, 1]
    logistic_first = fit_logistic(x, y, config).predict_proba(TABLE.features)[:, 1]
    logistic_second = fit_logistic(x, y, config).predict_proba(TABLE.features)[:, 1]

    assert np.array_equal(first, second)
    assert np.array_equal(logistic_first, logistic_second)


def test_two_full_runs_give_the_same_test_probabilities_and_choice() -> None:
    config = quick_config()

    first = run_research(TABLE, config)
    second = run_research(TABLE, config)

    assert first.chosen == second.chosen
    for name in ("logistic", "lightgbm"):
        assert np.array_equal(first.test_probabilities[name], second.test_probabilities[name])
    assert first.tuned_threshold == second.tuned_threshold
    assert set(first.test_costs) == {
        "never",
        "always",
        "business_rule",
        "tuned_threshold",
        "cost_rule",
    }


def test_the_choice_and_the_test_follow_the_split() -> None:
    result = run_research(TABLE, quick_config())
    split = split_by_month(TABLE.claimed_at, quick_config().split)

    assert result.test_claims == int(split.test.sum())
    assert result.test_frauds == int(TABLE.is_fraud[split.test].sum())
    assert result.chosen in {"logistic", "lightgbm"}
    assert result.test["business_rule"].brier is None
    assert result.test["logistic"].brier is not None
    assert 0.0 <= result.tuned_threshold <= 1.0


def test_a_model_that_does_not_beat_the_business_rule_stops_the_study() -> None:
    claims_30d = TABLE.features[:, FEATURE_NAMES.index("claims_30d")]
    exactly_the_rule = TABLE.__class__(
        claim_ids=TABLE.claim_ids,
        claimed_at=TABLE.claimed_at,
        contexts=(),
        features=TABLE.features,
        is_fraud=(claims_30d >= 3).astype(np.int64),
        requested_cents=TABLE.requested_cents,
    )

    with pytest.raises(ResearchStop) as stopped:
        run_research(exactly_the_rule, quick_config())

    assert stopped.value.rule == "research.model_below_business_rule"


def test_the_business_rule_flags_three_or_more_recent_claims() -> None:
    features = np.zeros((3, len(FEATURE_NAMES)))
    features[:, FEATURE_NAMES.index("claims_30d")] = [2, 3, 7]

    assert business_rule(features, 3).tolist() == [False, True, True]


def test_the_exported_logistic_matches_scikit_learn() -> None:
    """The "tiny model trained in the test": the application's numbers against scikit-learn's."""
    config = quick_config()
    split = split_by_month(TABLE.claimed_at, config.split)
    x = TABLE.features
    pipeline = fit_logistic(x[split.train], TABLE.is_fraud[split.train], config)
    calibrated = CalibratedClassifierCV(FrozenEstimator(pipeline), method="sigmoid").fit(
        x[split.validation], TABLE.is_fraud[split.validation]
    )
    candidate = candidate_from(pipeline, calibrated, config.review_cost_cents)
    model = candidate.to_model("test")

    explained = [model.logistic.explain(tuple(row)) for row in x[split.test]]
    raw = np.array([value for value, _ in explained])
    contributions = np.array([sum(parts) for _, parts in explained])
    served = np.array([model.calibrator.probability(value) for value in raw])
    expected = calibrated.predict_proba(x[split.test])[:, 1]

    assert np.max(np.abs(raw - pipeline.decision_function(x[split.test]))) < 1e-6
    assert np.max(np.abs(contributions + model.logistic.intercept - raw)) < 1e-9
    assert_close(served, expected)
    a, b = candidate.calibrator.a, candidate.calibrator.b
    assert_close(sigmoid_by_hand(pipeline.decision_function(x[split.test]), a, b), expected)


def test_a_mismatch_beyond_the_tolerance_is_reported() -> None:
    with pytest.raises(ProbabilityMismatchError, match="differ by"):
        assert_close(np.array([0.5]), np.array([0.5 + 1e-6]))
    with pytest.raises(ProbabilityMismatchError):
        assert_close(np.array([np.nan]), np.array([0.5]))


def test_the_candidate_folder_round_trips(tmp_path: Path) -> None:
    folder = tmp_path / "candidate"
    config = quick_config()
    pipeline = fit_logistic(TABLE.features, TABLE.is_fraud, config)
    calibrated = CalibratedClassifierCV(FrozenEstimator(pipeline), method="sigmoid").fit(
        TABLE.features, TABLE.is_fraud
    )
    candidate = candidate_from(pipeline, calibrated, 400)

    write_candidate(folder, candidate, {"chosen": "logistic"}, {})
    write_candidate(folder, candidate, {"chosen": "logistic", "again": True}, {})
    again, research = read_candidate(folder)

    assert again == candidate
    assert research == {"chosen": "logistic", "again": True}
    assert canonical_json({"b": 1, "a": 2}) == b'{\n  "a": 2,\n  "b": 1\n}\n'
