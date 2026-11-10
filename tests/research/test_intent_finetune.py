"""The parts of the fine-tuning script that need no torch: they run in CI."""

import pytest

from research.intent.config import load_config
from research.intent.data import LABELS
from research.intent.finetune import (
    RunSettings,
    best_rate,
    choose_max_length,
    label_maps,
    long_share,
)

SECTION = load_config().finetune


def test_the_label_maps_follow_the_study_order() -> None:
    id2label, label2id = label_maps()

    assert [id2label[i] for i in range(len(LABELS))] == list(LABELS)
    assert all(label2id[label] == i for i, label in id2label.items())


def test_128_tokens_unless_more_than_5_percent_of_validation_is_longer() -> None:
    five_percent = [129] * 5 + [20] * 95
    six_percent = [129] * 6 + [20] * 94

    assert long_share(five_percent, 128) == pytest.approx(0.05)
    assert choose_max_length(five_percent, SECTION) == 128
    assert choose_max_length(six_percent, SECTION) == 256
    assert choose_max_length([128] * 10, SECTION) == 128
    assert long_share([], 128) == 0.0


def test_the_run_settings_carry_the_config_and_name_the_folder() -> None:
    settings = RunSettings.of(SECTION, 3e-5, 7, 128)

    assert settings.name == "lr3e-05-seed7"
    assert (settings.batch_size, settings.max_epochs, settings.patience) == (16, 4, 1)
    assert settings.base_revision == SECTION.base_revision
    assert (settings.warmup_ratio, settings.weight_decay) == (0.1, 0.01)


def test_the_best_rate_wins_and_a_tie_keeps_the_smaller_one() -> None:
    results = [
        {"learning_rate": 5e-5, "macro_f1": 0.90},
        {"learning_rate": 2e-5, "macro_f1": 0.93},
        {"learning_rate": 3e-5, "macro_f1": 0.93},
    ]

    assert best_rate(results) == 2e-5
