"""Properties that hold for any order, proposal and history (Hypothesis)."""

from dataclasses import replace

from hypothesis import given
from hypothesis import strategies as st
from strategies import ITEM_REASONS, Scenario, prior_actions, scenarios
from support.policy_builders import executed

from deliverius.policy import (
    ActionKind,
    Allowed,
    ApprovedBy,
    ClaimReason,
    CustomerHistory,
    DenialReason,
    Denied,
    NeedsApproval,
    PolicyDecision,
    evaluate,
)
from deliverius.shared import Money

MONEY_BACK = (ActionKind.REFUND, ActionKind.CANCELLATION)
ONCE_ONLY = (*ITEM_REASONS, ClaimReason.LATE_DELIVERY)


def decide(scenario: Scenario) -> PolicyDecision:
    return evaluate(scenario.action, scenario.order, scenario.history, scenario.claimed_at)


def refunded_on_order(scenario: Scenario) -> Money:
    return Money.sum(
        prior.amount
        for prior in scenario.history.actions
        if prior.order_id == scenario.order.id and prior.kind in MONEY_BACK
    )


@given(scenarios())
def test_a_decision_that_is_not_denied_stays_within_the_ceiling(scenario: Scenario) -> None:
    decision = decide(scenario)
    if not isinstance(decision, Denied):
        assert scenario.action.amount <= decision.ceiling


@given(scenarios())
def test_money_back_never_exceeds_the_order_total(scenario: Scenario) -> None:
    decision = decide(scenario)
    if not isinstance(decision, Denied) and scenario.action.kind in MONEY_BACK:
        assert refunded_on_order(scenario) + scenario.action.amount <= scenario.order.total


@given(scenarios())
def test_same_inputs_give_the_same_decision(scenario: Scenario) -> None:
    assert decide(scenario) == decide(scenario)


@given(scenarios(), st.integers(1, 10_000))
def test_a_larger_amount_than_a_denied_one_is_denied_too(scenario: Scenario, extra: int) -> None:
    decision = decide(scenario)
    if isinstance(decision, Denied) and decision.reason is DenialReason.EXCEEDS_CEILING:
        larger = replace(scenario.action, amount=scenario.action.amount + Money(extra))
        assert decide(replace(scenario, action=larger)) == decision


@given(st.data())
def test_more_auto_approved_history_never_turns_approval_into_allowed(data: st.DataObject) -> None:
    scenario = data.draw(scenarios())
    if not isinstance(decide(scenario), NeedsApproval):
        return
    extra = data.draw(prior_actions(scenario.order, scenario.claimed_at))
    more = CustomerHistory(
        (*scenario.history.actions, replace(extra, approved_by=ApprovedBy.AUTOMATIC))
    )
    assert not isinstance(decide(replace(scenario, history=more)), Allowed)


@given(scenarios(plausible=True))
def test_an_allowed_proposal_is_not_allowed_again_once_executed(scenario: Scenario) -> None:
    """For the same items, and for a delay (compensated once per order).

    Whole-order refunds may come in parts and are bounded by the total instead (see above); an
    executed cancellation also changes the order status, which this test does not replay.
    """
    if scenario.action.reason not in ONCE_ONLY or not isinstance(decide(scenario), Allowed):
        return
    done = executed(scenario.action, scenario.order, scenario.claimed_at, ApprovedBy.AUTOMATIC)
    again = replace(scenario, history=CustomerHistory((*scenario.history.actions, done)))
    assert not isinstance(decide(again), Allowed)
