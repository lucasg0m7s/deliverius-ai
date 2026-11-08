from datetime import timedelta

import pytest

from deliverius.policy import POLICY_LIMITS, PolicyLimits
from deliverius.shared import InvariantError, Money


def test_defaults_match_the_policy_tables() -> None:
    """The numbers of docs/modelo-dominio.md, section 3 (rules and auto-approval limits)."""
    assert PolicyLimits() == POLICY_LIMITS
    assert POLICY_LIMITS.item_claim_window == timedelta(hours=24)
    assert POLICY_LIMITS.late_claim_window == timedelta(hours=24)
    assert POLICY_LIMITS.not_delivered_claim_window == timedelta(hours=48)
    assert POLICY_LIMITS.duplicate_charge_claim_window == timedelta(days=90)
    assert POLICY_LIMITS.quality_refund_percent == 50
    assert POLICY_LIMITS.minor_delay == timedelta(minutes=30)
    assert POLICY_LIMITS.major_delay == timedelta(minutes=60)
    assert POLICY_LIMITS.minor_delay_coupon_percent == 10
    assert POLICY_LIMITS.minor_delay_coupon_cap == Money(1_500)
    assert POLICY_LIMITS.major_delay_coupon == Money(2_000)
    assert POLICY_LIMITS.auto_approval_limit == Money(5_000)
    assert POLICY_LIMITS.coupon_auto_approval_limit == Money(2_000)
    assert POLICY_LIMITS.customer_auto_approval_limit == Money(10_000)
    assert POLICY_LIMITS.customer_auto_approval_window == timedelta(days=30)


@pytest.mark.parametrize("percent", [-1, 101])
@pytest.mark.parametrize("field", ["quality_refund_percent", "minor_delay_coupon_percent"])
def test_percent_outside_0_to_100_is_rejected(field: str, percent: int) -> None:
    with pytest.raises(InvariantError) as error:
        PolicyLimits(**{field: percent})  # type: ignore[arg-type]
    assert error.value.rule == "policy_limits.percent_out_of_range"


@pytest.mark.parametrize("percent", [0, 100])
def test_percent_bounds_are_accepted(percent: int) -> None:
    assert PolicyLimits(quality_refund_percent=percent).quality_refund_percent == percent


@pytest.mark.parametrize("window", [timedelta(0), timedelta(hours=-1)])
@pytest.mark.parametrize(
    "field",
    ["item_claim_window", "not_delivered_claim_window", "customer_auto_approval_window"],
)
def test_non_positive_window_is_rejected(field: str, window: timedelta) -> None:
    with pytest.raises(InvariantError) as error:
        PolicyLimits(**{field: window})  # type: ignore[arg-type]
    assert error.value.rule == "policy_limits.window_not_positive"


@pytest.mark.parametrize("minor_delay", [timedelta(minutes=60), timedelta(minutes=61)])
def test_minor_delay_must_be_below_major_delay(minor_delay: timedelta) -> None:
    with pytest.raises(InvariantError) as error:
        PolicyLimits(minor_delay=minor_delay)
    assert error.value.rule == "policy_limits.minor_delay_not_below_major"
