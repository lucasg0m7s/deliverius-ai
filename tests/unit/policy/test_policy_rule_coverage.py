"""Each row of the policy tables (docs/modelo-dominio.md, section 3) names the tests proving it."""

import importlib

import pytest

ENGINE_TESTS = "test_policy_engine"
LIMITS_TESTS = "test_policy_limits"

# Row of the table → tests in the engine test module that prove it (resolution, ceiling and window).
RULES: dict[str, tuple[str, ...]] = {
    "Item faltando": (
        "TestMissingItem::test_refund_is_capped_at_item_price",
        "TestMissingItem::test_ceiling_is_unit_price_times_quantity",
        "TestItemClaimWindow::test_claim_one_second_after_the_window_expires",
    ),
    "Item errado": (
        "TestWrongItem::test_refund_is_capped_at_item_price",
        "TestItemClaimWindow::test_claim_one_second_after_the_window_expires",
    ),
    "Qualidade": (
        "TestQualityIssue::test_ceiling_is_half_the_claimed_items",
        "TestQualityIssue::test_quality_coupon_above_coupon_limit_needs_approval",
        "TestItemClaimWindow::test_claim_one_second_after_the_window_expires",
    ),
    "Atraso de 30 a 60 min": (
        "TestLateDelivery::test_minor_delay_coupon_is_ten_percent_capped",
        "TestLateDelivery::test_minor_delay_coupon_is_ten_percent_of_the_total",
        "TestLateDelivery::test_delay_below_minimum_is_denied",
        "TestLateDelivery::test_claim_window_after_delivery_edge",
    ),
    "Atraso de mais de 60 min": (
        "TestLateDelivery::test_major_delay_allows_coupon_or_cancellation_before_dispatch",
        "TestLateDelivery::test_cancellation_after_dispatch_is_denied_but_coupon_still_fits",
        "TestLateDelivery::test_open_order_has_no_claim_window",
        "TestLateDelivery::test_delay_is_compensated_once_per_order",
    ),
    "Não entregue (sem evento entregue)": (
        "TestNotDelivered::test_failed_delivery_ceiling_is_remaining_total",
        "TestNotDelivered::test_failed_delivery_window_edge",
        "TestNotDelivered::test_order_in_progress_is_a_delay_case",
    ),
    "Não entregue, mas marcado como entregue": (
        "TestNotDelivered::test_marked_as_delivered_always_needs_approval",
        "TestNotDelivered::test_marked_as_delivered_window_edge",
    ),
    "Cobrança duplicada": (
        "TestDuplicateCharge::test_always_needs_approval",
        "TestDuplicateCharge::test_window_counts_from_placement",
    ),
    "Cancelamento antes de o restaurante aceitar": (
        "TestCustomerCancellation::test_before_acceptance_is_allowed_whatever_the_amount",
        "TestCustomerCancellation::test_before_acceptance_ignores_the_customer_limit",
    ),
    "Cancelamento depois de aceito": (
        "TestCustomerCancellation::test_after_acceptance_needs_approval",
        "TestCustomerCancellation::test_after_dispatch_is_denied",
    ),
    # Routed to a person before a proposal exists: the engine must have no action for them.
    "Alergia, segurança alimentar, mal-estar": (
        "TestSensitiveCases::test_engine_has_no_reason_for_sensitive_cases",
    ),
    "Conduta do entregador, assédio, acidente": (
        "TestSensitiveCases::test_engine_has_no_reason_for_sensitive_cases",
    ),
}

AUTO_APPROVAL_LIMITS: dict[str, tuple[str, ...]] = {
    "Valor por ação": (
        "TestAutoApprovalLimits::test_refund_above_action_limit_needs_approval",
        "TestAutoApprovalLimits::test_late_cancellation_above_action_limit_needs_approval",
    ),
    "Valor autoaprovado por cliente em 30 dias": (
        "TestAutoApprovalLimits::test_customer_limit_counts_auto_approved_in_window",
        "TestAutoApprovalLimits::test_customer_limit_window_edge",
        "TestAutoApprovalLimits::test_reviewer_approved_actions_do_not_count",
    ),
    "Cupom por ação": ("TestAutoApprovalLimits::test_coupon_above_coupon_limit_needs_approval",),
}

# Rows that are deliberately not decided by this engine, with where they are decided.
OUT_OF_ENGINE = {"Nota de fraude": "fraud score rule (probability x amount), a later change"}


def test_every_table_row_is_listed() -> None:
    assert len(RULES) == 12
    assert len(AUTO_APPROVAL_LIMITS) + len(OUT_OF_ENGINE) == 4


@pytest.mark.parametrize(
    "test_name",
    sorted({name for names in (*RULES.values(), *AUTO_APPROVAL_LIMITS.values()) for name in names}),
)
def test_listed_test_exists(test_name: str) -> None:
    module = importlib.import_module(ENGINE_TESTS)
    class_name, method_name = test_name.split("::")
    assert callable(getattr(getattr(module, class_name, None), method_name, None)), test_name


def test_default_numbers_are_checked() -> None:
    module = importlib.import_module(LIMITS_TESTS)
    assert callable(module.test_defaults_match_the_policy_tables)
