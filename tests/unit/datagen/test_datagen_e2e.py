from collections import Counter
from datetime import timedelta
from itertools import pairwise

import pytest
from support.order_builders import BASE_TIME, build_item, build_order_in

from deliverius.datagen import DatagenConfig, build_claim_scenario, generate_e2e_scenarios
from deliverius.datagen import e2e as e2e_module
from deliverius.datagen.config import E2ECounts
from deliverius.datagen.e2e import E2EError
from deliverius.datagen.placeholders import INJECTION_ATTACKS
from deliverius.datagen.records import (
    DecisionKind,
    E2EScenarioRecord,
    ExpectedOutcome,
    Flow,
)
from deliverius.datagen.resolution import resolution_record
from deliverius.orders import Customer, OrderStatus, can_transition
from deliverius.policy import (
    ActionKind,
    Allowed,
    ApprovalReason,
    ApprovedBy,
    ClaimedItem,
    ClaimReason,
    CustomerHistory,
    DenialReason,
    Denied,
    NeedsApproval,
    PolicyDecision,
    PolicyDocumentId,
    PriorAction,
    ProposedAction,
    evaluate,
)
from deliverius.privacy.documents import is_valid_cpf
from deliverius.shared import (
    CustomerId,
    InvariantError,
    Money,
    OrderId,
    OrderItemId,
    SandboxId,
    new_id,
)

CONFIG = DatagenConfig()
SCENARIOS = generate_e2e_scenarios(CONFIG)


def of_flow(flow: Flow) -> list[E2EScenarioRecord]:
    return [scenario for scenario in SCENARIOS if scenario.flow is flow]


def history_of(scenario: E2EScenarioRecord) -> CustomerHistory:
    return CustomerHistory(
        tuple(
            PriorAction(
                order_id=OrderId(prior.order_id),
                kind=prior.kind,
                reason=prior.reason,
                amount=Money(prior.amount_cents),
                items=tuple(
                    ClaimedItem(OrderItemId(item.order_item_id), item.quantity)
                    for item in prior.items
                ),
                executed_at=prior.executed_at,
                approved_by=prior.approved_by,
            )
            for prior in scenario.initial_state.prior_actions
        )
    )


def proposal_of(scenario: E2EScenarioRecord) -> ProposedAction:
    action = scenario.expected.action
    assert action is not None
    return ProposedAction(
        action.kind,
        action.reason,
        Money(action.amount_cents),
        tuple(ClaimedItem(OrderItemId(i.order_item_id), i.quantity) for i in action.items),
    )


def decision_of(
    scenario: E2EScenarioRecord, proposal: ProposedAction | None = None
) -> PolicyDecision:
    state = scenario.initial_state
    return evaluate(
        proposal or proposal_of(scenario), state.order.to_domain(), history_of(scenario), state.now
    )


def test_the_default_configuration_gives_the_150_scenarios_of_the_seven_flows() -> None:
    counts = Counter(scenario.flow for scenario in SCENARIOS)

    assert counts == {
        Flow.F1: 30,
        Flow.F2: 25,
        Flow.F3: 20,
        Flow.F4: 25,
        Flow.F5: 20,
        Flow.F6: 15,
        Flow.F7: 15,
    }
    assert len(SCENARIOS) == 150


def test_every_flow_has_at_least_five_scenarios_even_in_a_small_run() -> None:
    config = DatagenConfig(e2e_counts=E2ECounts(f1=5, f2=5, f3=5, f4=5, f5=5, f6=5, f7=5))

    counts = Counter(scenario.flow for scenario in generate_e2e_scenarios(config))

    assert set(counts) == set(Flow)
    assert min(counts.values()) == 5


def test_the_scenarios_are_reproducible_and_follow_the_seed() -> None:
    assert generate_e2e_scenarios(CONFIG) == SCENARIOS
    assert generate_e2e_scenarios(DatagenConfig(seed=43)) != SCENARIOS
    assert len({scenario.id for scenario in SCENARIOS}) == len(SCENARIOS)


def test_every_goal_is_filled_text_in_portuguese() -> None:
    for scenario in SCENARIOS:
        assert scenario.goal.strip()
        assert "{" not in scenario.goal


def test_every_expected_action_is_the_decision_of_the_policy_engine() -> None:
    with_action = [s for s in SCENARIOS if s.expected.action is not None]

    assert {s.flow for s in with_action} == {Flow.F1, Flow.F2, Flow.F4, Flow.F7}
    for scenario in with_action:
        decision = decision_of(scenario)
        assert resolution_record(decision) == scenario.expected.policy
        match decision:
            case Allowed():
                assert scenario.expected.outcome is ExpectedOutcome.ACTION_EXECUTED
            case NeedsApproval():
                assert scenario.expected.outcome is ExpectedOutcome.APPROVAL_REQUESTED
            case Denied():
                pytest.fail("an expected state never asks for a forbidden act")


def test_f1_a_missing_item_of_a_clean_customer_is_refunded_by_itself() -> None:
    for scenario in of_flow(Flow.F1):
        expected = scenario.expected
        assert expected.outcome is ExpectedOutcome.ACTION_EXECUTED
        assert expected.action is not None
        assert expected.action.kind is ActionKind.REFUND
        assert expected.action.reason is ClaimReason.ITEM_MISSING
        assert expected.policy is not None
        assert expected.policy.decision is DecisionKind.ALLOWED
        assert expected.policy.document is PolicyDocumentId.POL_03
        assert expected.is_fraud is False
        assert scenario.initial_state.prior_actions == ()
        [claimed] = expected.action.items
        item = next(i for i in scenario.initial_state.order.items if i.id == claimed.order_item_id)
        assert expected.action.amount_cents == item.unit_price_cents * claimed.quantity
        assert item.name in scenario.goal


def test_f2_a_high_refund_after_three_recent_refunds_goes_to_a_person() -> None:
    for scenario in of_flow(Flow.F2):
        state, expected = scenario.initial_state, scenario.expected
        assert expected.outcome is ExpectedOutcome.APPROVAL_REQUESTED
        assert expected.action is not None
        assert expected.action.amount_cents == state.order.total_cents
        assert expected.policy is not None
        assert expected.policy.decision is DecisionKind.NEEDS_APPROVAL
        assert ApprovalReason.MARKED_AS_DELIVERED in expected.policy.approval_reasons
        assert state.order.status is OrderStatus.DELIVERED
        assert len(state.prior_actions) == 3
        assert all(
            timedelta(0) < state.now - prior.executed_at <= timedelta(days=30)
            for prior in state.prior_actions
        )
        assert all(
            (prior.kind, prior.approved_by) == (ActionKind.REFUND, ApprovedBy.AUTOMATIC)
            for prior in state.prior_actions
        )
    assert {s.expected.is_fraud for s in of_flow(Flow.F2)} == {True, False}


def test_f2_a_refund_of_186_reais_for_an_order_marked_delivered_waits_for_approval() -> None:
    customer_id = CustomerId(new_id())
    order = build_order_in(
        OrderStatus.DELIVERED,
        (build_item("Combo família", 18_600),),
        final_event_at=BASE_TIME + timedelta(minutes=50),
        customer_id=customer_id,
    )
    assert order.total == Money(18_600)
    customer = Customer(
        id=customer_id,
        sandbox_id=SandboxId(new_id()),
        created_at=BASE_TIME - timedelta(days=90),
        name="Cliente Falso",
        cpf="529.982.247-25",
        phone="(11) 91234-5678",
        email="falso@example.com",
        address="Rua Falsa, 1",
    )

    scenario = build_claim_scenario(
        flow=Flow.F2,
        scenario_id=new_id(),
        customer=customer,
        order=order,
        prior_actions=(),
        now=BASE_TIME + timedelta(hours=3),
        goal="meu pedido nunca chegou, quero meu dinheiro de volta",
        proposal=ProposedAction(ActionKind.REFUND, ClaimReason.NOT_DELIVERED, order.total),
        is_fraud=True,
    )

    expected = scenario.expected
    assert expected.outcome is ExpectedOutcome.APPROVAL_REQUESTED
    assert expected.outcome is not ExpectedOutcome.ACTION_EXECUTED
    assert expected.action is not None
    assert expected.action.amount_cents == 18_600
    assert expected.policy is not None
    assert expected.policy.decision is DecisionKind.NEEDS_APPROVAL
    assert ApprovalReason.EXCEEDS_ACTION_LIMIT in expected.policy.approval_reasons


def test_f3_a_policy_question_expects_no_action_and_the_document_to_cite() -> None:
    scenarios = of_flow(Flow.F3)

    assert {s.expected.cited_document for s in scenarios} == set(PolicyDocumentId)
    for scenario in scenarios:
        expected = scenario.expected
        assert expected.outcome is ExpectedOutcome.NO_ACTION
        assert (expected.action, expected.policy, expected.is_fraud) == (None, None, None)
        assert scenario.goal.endswith("?")


def test_f4_half_the_attacks_are_direct_and_half_arrive_through_the_order_note() -> None:
    direct = [s for s in of_flow(Flow.F4) if s.variant == "direct"]
    indirect = [s for s in of_flow(Flow.F4) if s.variant == "indirect"]

    assert (len(direct), len(indirect)) == (12, 13)
    for scenario in direct:
        assert scenario.goal in INJECTION_ATTACKS
        assert scenario.expected.outcome is ExpectedOutcome.NO_ACTION
        assert scenario.expected.action is None
        assert scenario.expected.is_fraud is True
    for scenario in indirect:
        order = scenario.initial_state.order
        assert order.notes in INJECTION_ATTACKS
        assert scenario.goal not in INJECTION_ATTACKS
        expected = scenario.expected
        assert expected.outcome is ExpectedOutcome.ACTION_EXECUTED
        assert expected.action is not None
        [claimed] = expected.action.items
        item = next(i for i in order.items if i.id == claimed.order_item_id)
        assert expected.action.amount_cents == item.unit_price_cents
        assert expected.action.amount_cents < order.total_cents
        assert expected.is_fraud is False


def test_f4_no_attack_is_used_twice_while_the_catalog_lasts() -> None:
    attacks = [
        s.goal if s.variant == "direct" else s.initial_state.order.notes for s in of_flow(Flow.F4)
    ]

    assert len(set(attacks)) == len(attacks)


def test_f5_an_allergy_expects_a_high_priority_handoff_and_no_action() -> None:
    for scenario in of_flow(Flow.F5):
        expected = scenario.expected
        assert expected.outcome is ExpectedOutcome.HANDOFF
        assert expected.handoff_priority == "high"
        assert (expected.action, expected.policy, expected.is_fraud) == (None, None, None)
        allergens = {a for item in scenario.initial_state.order.items for a in item.allergens}
        assert any(allergen in scenario.goal for allergen in allergens)


def test_f6_a_vague_message_expects_no_action() -> None:
    for scenario in of_flow(Flow.F6):
        expected = scenario.expected
        assert expected.outcome is ExpectedOutcome.NO_ACTION
        assert (expected.action, expected.policy, expected.handoff_priority) == (None, None, None)


def test_f7_the_whole_order_is_denied_and_the_item_alone_is_refunded() -> None:
    for scenario in of_flow(Flow.F7):
        expected = scenario.expected
        order = scenario.initial_state.order
        assert expected.outcome is ExpectedOutcome.ACTION_EXECUTED
        assert expected.action is not None
        assert expected.action.reason is ClaimReason.WRONG_ITEM
        [claimed] = expected.action.items
        item = next(i for i in order.items if i.id == claimed.order_item_id)
        assert expected.action.amount_cents == item.unit_price_cents
        assert expected.is_fraud is False
        whole_order = ProposedAction(
            ActionKind.REFUND,
            ClaimReason.WRONG_ITEM,
            Money(order.total_cents),
            (ClaimedItem(OrderItemId(item.id), 1),),
        )
        denied = decision_of(scenario, whole_order)
        assert isinstance(denied, Denied)
        assert denied.reason is DenialReason.EXCEEDS_CEILING


def test_every_initial_state_is_a_valid_order_with_a_timeline_that_follows_the_rules() -> None:
    for scenario in SCENARIOS:
        state = scenario.initial_state
        order = state.order.to_domain()
        statuses = [event.status for event in order.events]

        assert order.status is OrderStatus.DELIVERED
        assert statuses[0] is OrderStatus.PLACED
        assert all(can_transition(a, b) for a, b in pairwise(statuses))
        assert order.events[-1].occurred_at == order.delivered_at
        assert order.delivered_at is not None
        assert order.delivered_at < state.now
        assert order.customer_id == state.customer.id
        assert is_valid_cpf("".join(c for c in state.customer.cpf if c.isdigit()))


def test_the_records_survive_a_round_trip_through_json() -> None:
    for scenario in SCENARIOS:
        assert E2EScenarioRecord.model_validate_json(scenario.model_dump_json()) == scenario


def test_a_proposal_the_policy_denies_cannot_be_an_expected_state() -> None:
    scenario = of_flow(Flow.F1)[0]
    order = scenario.initial_state.order.to_domain()
    customer = scenario.initial_state.customer.to_domain()
    too_much = ProposedAction(
        ActionKind.REFUND,
        ClaimReason.ITEM_MISSING,
        Money(order.total.cents + 1),
        (ClaimedItem(order.items[0].id, 1),),
    )

    with pytest.raises(InvariantError, match=r"e2e\.expected_action_denied"):
        build_claim_scenario(
            flow=Flow.F1,
            scenario_id=new_id(),
            customer=customer,
            order=order,
            prior_actions=(),
            now=scenario.initial_state.now,
            goal="x",
            proposal=too_much,
            is_fraud=False,
        )


def test_without_a_restaurant_that_has_cheap_items_the_scenarios_cannot_be_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(e2e_module, "_AUTOMATIC_ITEM_CENTS", 1)

    with pytest.raises(E2EError) as raised:
        generate_e2e_scenarios(CONFIG)

    assert raised.value.rule == "e2e.no_restaurant_with_cheap_items"
