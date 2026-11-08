"""The single source of every policy number: the engine decides with it, the documents show it."""

from dataclasses import dataclass, fields
from datetime import timedelta
from typing import Final

from deliverius.shared.errors import InvariantError
from deliverius.shared.money import Money


@dataclass(frozen=True, slots=True)
class PolicyLimits:
    """Field names double as document placeholders (`{{auto_approval_limit}}`).

    Every `int` field is a percentage and every `timedelta` a window or delay, which is how the
    documents know how to format them.
    """

    item_claim_window: timedelta = timedelta(hours=24)
    late_claim_window: timedelta = timedelta(hours=24)
    not_delivered_claim_window: timedelta = timedelta(hours=48)
    duplicate_charge_claim_window: timedelta = timedelta(days=90)
    quality_refund_percent: int = 50
    minor_delay: timedelta = timedelta(minutes=30)
    major_delay: timedelta = timedelta(minutes=60)
    minor_delay_coupon_percent: int = 10
    minor_delay_coupon_cap: Money = Money(1_500)
    major_delay_coupon: Money = Money(2_000)
    auto_approval_limit: Money = Money(5_000)
    coupon_auto_approval_limit: Money = Money(2_000)
    customer_auto_approval_limit: Money = Money(10_000)
    customer_auto_approval_window: timedelta = timedelta(days=30)
    # Not used to decide: they exist so POL-11 never writes a number by hand.
    instant_refund_credit_time: timedelta = timedelta(days=1)
    card_refund_credit_time: timedelta = timedelta(days=10)

    def __post_init__(self) -> None:
        for limit in fields(self):
            value = getattr(self, limit.name)
            if isinstance(value, int) and not 0 <= value <= 100:
                raise InvariantError("policy_limits.percent_out_of_range")
            if isinstance(value, timedelta) and value <= timedelta(0):
                raise InvariantError("policy_limits.window_not_positive")
        if self.minor_delay >= self.major_delay:
            raise InvariantError("policy_limits.minor_delay_not_below_major")


POLICY_LIMITS: Final = PolicyLimits()
