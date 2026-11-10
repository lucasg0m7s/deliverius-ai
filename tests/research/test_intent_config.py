"""The configuration holds the rule written in the design before any run."""

from research.dataset import load_dataset_pin
from research.intent.config import load_config

CONFIG = load_config()


def test_the_choice_rule_is_the_one_in_the_design() -> None:
    rule = CONFIG.rule

    assert (rule.min_sensitive_recall, rule.max_p95_ms) == (0.95, 100.0)
    assert (rule.bootstrap_resamples, rule.confidence_level) == (2000, 0.95)


def test_calibration_router_and_latency_settings() -> None:
    assert CONFIG.calibration.ece_bins == 15
    assert CONFIG.router.max_error == 0.05
    assert CONFIG.latency.warmup_messages == 20
    assert CONFIG.latency.intra_op_threads == 1


def test_the_llm_sample_and_the_finetune_rule() -> None:
    assert CONFIG.llm_baseline.sample_per_intent == 20
    assert CONFIG.llm_baseline.examples_per_intent == 2
    finetune = CONFIG.finetune
    assert (finetune.short_max_length, finetune.long_max_length) == (128, 256)
    assert finetune.long_share_threshold == 0.05
    assert finetune.learning_rates == (2e-5, 3e-5, 5e-5)
    assert len(finetune.seeds) == 3


def test_the_intent_research_reads_the_shared_dataset_pin() -> None:
    assert CONFIG.dataset == load_dataset_pin()
    assert CONFIG.model.repository == "G0m7s/deliverius-intent"
