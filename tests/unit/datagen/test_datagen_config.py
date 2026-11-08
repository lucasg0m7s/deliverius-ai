from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from deliverius.datagen import DATAGEN_VERSION, DatagenConfig
from deliverius.datagen.config import ClaimRates, IncidentRates, MessageConfig, ProfileMix


def test_the_defaults_are_the_documented_world() -> None:
    config = DatagenConfig()

    assert DATAGEN_VERSION == "1.0.0"
    assert (config.customer_count, config.restaurant_count, config.order_count) == (
        2_000,
        60,
        20_000,
    )
    assert config.reference_instant == datetime(2026, 10, 31, 23, 59, 59, tzinfo=UTC)
    assert config.period_start == datetime(2026, 5, 1, tzinfo=UTC)
    assert config.profile_mix == ProfileMix(honest=0.90, opportunist=0.07, fraudster=0.03)
    assert config.claim_rates.label_noise == 0.025


def test_the_antifraude_test_month_is_the_last_month_of_the_period() -> None:
    assert DatagenConfig().fraud_test_cutoff == datetime(2026, 10, 1, tzinfo=UTC)


def test_orders_stop_early_enough_for_every_claim_to_fit_in_the_period() -> None:
    config = DatagenConfig()

    assert config.orders_end < config.reference_instant


def test_the_profile_mix_must_add_up_to_one() -> None:
    with pytest.raises(ValidationError, match=r"profile_mix\.must_sum_to_one"):
        ProfileMix(honest=0.5, opportunist=0.07, fraudster=0.03)


@pytest.mark.parametrize("noise", [0.0, 0.019, 0.031, 0.5])
def test_label_noise_outside_two_to_three_percent_is_refused(noise: float) -> None:
    with pytest.raises(ValidationError):
        ClaimRates(label_noise=noise)


@pytest.mark.parametrize("noise", [0.02, 0.025, 0.03])
def test_label_noise_inside_the_range_is_accepted(noise: float) -> None:
    assert ClaimRates(label_noise=noise).label_noise == noise


def test_a_naive_reference_instant_is_refused() -> None:
    with pytest.raises(ValidationError, match="naive_datetime"):
        DatagenConfig(reference_instant=datetime(2026, 10, 31, 23, 59, 59))  # noqa: DTZ001


def test_an_offset_instant_is_stored_as_utc() -> None:
    config = DatagenConfig.model_validate({"period_start": "2026-05-01T03:00:00+03:00"})

    assert config.period_start == datetime(2026, 5, 1, tzinfo=UTC)
    assert config.period_start.utcoffset() == UTC.utcoffset(None)


def test_a_period_without_a_month_before_the_end_is_refused() -> None:
    with pytest.raises(ValidationError, match=r"datagen_config\.period_too_short"):
        DatagenConfig(period_start=datetime(2026, 10, 15, tzinfo=UTC))


def test_a_rate_above_one_is_refused() -> None:
    with pytest.raises(ValidationError):
        IncidentRates(missing_item=1.5)


def test_the_config_is_frozen_and_closed() -> None:
    config = DatagenConfig()

    with pytest.raises(ValidationError):
        config.seed = 1  # type: ignore[misc]
    with pytest.raises(ValidationError):
        DatagenConfig.model_validate({"unknown": 1})


def test_scaling_the_incident_rates_never_goes_past_one() -> None:
    scaled = IncidentRates(minor_delay=0.5, missing_item=0.03).scaled(4.0)

    assert scaled.minor_delay == 1.0
    assert scaled.missing_item == pytest.approx(0.12)


def test_the_message_defaults_are_the_documented_sizes() -> None:
    messages = DatagenConfig().messages

    assert messages.scenarios_per_intent * 11 == 297
    assert messages.holdout_scenarios_per_intent * 11 == 55
    assert (messages.variations_per_scenario, messages.max_variation_chars) == (10, 600)
    assert (messages.personal_data_rate, messages.injection_rate) == (0.30, 0.05)
    assert (messages.concurrency, messages.max_attempts, messages.rag_pair_target) == (4, 3, 60)
    assert isinstance(DatagenConfig(messages=MessageConfig(concurrency=1)).messages, MessageConfig)


def test_the_end_to_end_defaults_are_the_documented_counts() -> None:
    counts = DatagenConfig().e2e_counts

    assert (counts.f1, counts.f2, counts.f3, counts.f4, counts.f5, counts.f6, counts.f7) == (
        30,
        25,
        20,
        25,
        20,
        15,
        15,
    )
    assert sum(counts.model_dump().values()) == 150


@pytest.mark.parametrize(
    "field", ["scenarios_per_intent", "variations_per_scenario", "concurrency"]
)
def test_a_message_size_must_be_positive(field: str) -> None:
    with pytest.raises(ValidationError):
        MessageConfig(**{field: 0})
