"""What the generator is told: sizes, rates and the reference instant, all with defaults."""

from datetime import UTC, datetime, timedelta
from typing import Annotated, Final, Self

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, model_validator

from deliverius.datagen.records import UtcInstant

# Raise it when a change makes the same seed write different files; the golden hash test says so.
DATAGEN_VERSION: Final = "1.0.0"

# Claims reach up to 72 hours after the last order event: orders stop this far from the reference.
CLAIM_MARGIN: Final = timedelta(days=4)

type Rate = Annotated[float, Field(ge=0.0, le=1.0)]


class _Config(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ProfileMix(_Config):
    honest: Rate = 0.90
    opportunist: Rate = 0.07
    fraudster: Rate = 0.03

    @model_validator(mode="after")
    def _sums_to_one(self) -> Self:
        if abs(self.honest + self.opportunist + self.fraudster - 1.0) > 1e-9:
            raise ValueError("profile_mix.must_sum_to_one")
        return self


class IncidentRates(_Config):
    """Chance, per order, of each thing going wrong."""

    missing_item: Rate = 0.03
    wrong_item: Rate = 0.02
    quality_issue: Rate = 0.03
    minor_delay: Rate = 0.06
    major_delay: Rate = 0.02
    delivery_failed: Rate = 0.01
    marked_delivered_not_received: Rate = 0.005
    duplicate_charge: Rate = 0.003
    customer_cancellation: Rate = 0.02

    def scaled(self, factor: float) -> Self:
        return self.model_copy(
            update={
                name: min(1.0, getattr(self, name) * factor) for name in type(self).model_fields
            }
        )


class ClaimRates(_Config):
    honest_reports_incident: Rate = 0.7
    fraudster_invents: Rate = 0.3
    opportunist_exaggerates: Rate = 0.5
    opportunist_invents: Rate = 0.05
    unlucky_honest_share: Rate = 0.05
    unlucky_multiplier: Annotated[float, Field(ge=1.0)] = 4.0
    late_claim_share: Rate = 0.05
    reviewer_rejects_fraud: Rate = 0.7
    label_noise: Annotated[float, Field(ge=0.02, le=0.03)] = 0.025


class MessageConfig(_Config):
    """The message layer: how many scenarios, how many variations, how the quota is spent."""

    scenarios_per_intent: PositiveInt = 27
    holdout_scenarios_per_intent: PositiveInt = 5
    variations_per_scenario: PositiveInt = 10
    max_variation_chars: PositiveInt = 600
    personal_data_rate: Rate = 0.30
    injection_rate: Rate = 0.05
    concurrency: PositiveInt = 4
    max_attempts: PositiveInt = 3
    rag_pair_target: PositiveInt = 60


class E2ECounts(_Config):
    """How many end-to-end scenarios each flow of `docs/fluxos-do-agente.md` gets."""

    f1: PositiveInt = 30
    f2: PositiveInt = 25
    f3: PositiveInt = 20
    f4: PositiveInt = 25
    f5: PositiveInt = 20
    f6: PositiveInt = 15
    f7: PositiveInt = 15


class DatagenConfig(_Config):
    seed: int = 42
    reference_instant: UtcInstant = datetime(2026, 10, 31, 23, 59, 59, tzinfo=UTC)
    period_start: UtcInstant = datetime(2026, 5, 1, tzinfo=UTC)
    customer_count: PositiveInt = 2_000
    restaurant_count: PositiveInt = 60
    order_count: PositiveInt = 20_000
    profile_mix: ProfileMix = ProfileMix()
    incident_rates: IncidentRates = IncidentRates()
    claim_rates: ClaimRates = ClaimRates()
    messages: MessageConfig = MessageConfig()
    e2e_counts: E2ECounts = E2ECounts()

    @model_validator(mode="after")
    def _period_holds_a_test_month(self) -> Self:
        if not self.period_start < self.fraud_test_cutoff < self.orders_end:
            raise ValueError("datagen_config.period_too_short")
        return self

    @property
    def orders_end(self) -> datetime:
        return self.reference_instant - CLAIM_MARGIN

    @property
    def fraud_test_cutoff(self) -> datetime:
        """The start of the last month of the period: the antifraude test is what comes after."""
        reference = self.reference_instant
        return datetime(reference.year, reference.month, 1, tzinfo=UTC)
