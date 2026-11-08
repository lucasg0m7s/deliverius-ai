import json
from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from deliverius.datagen import World
from deliverius.datagen.records import (
    INTENT_OF_REASON,
    ClaimRecord,
    CustomerRecord,
    DecisionKind,
    Intent,
    Manifest,
    MessageRecord,
    OrderRecord,
    PiiKind,
    PiiSpan,
    ResolutionRecord,
    RestaurantRecord,
    Split,
)
from deliverius.policy import ApprovalReason, ClaimReason, DenialReason, PolicyDocumentId


def customer_record(created_at: datetime) -> CustomerRecord:
    return CustomerRecord(
        id=uuid4(),
        sandbox_id=uuid4(),
        created_at=created_at,
        name="n",
        cpf="c",
        phone="p",
        email="e",
        address="a",
    )


def test_an_order_survives_the_trip_through_its_record(small_world: World) -> None:
    for sim in small_world.orders:
        record = OrderRecord.from_domain(sim.order)

        assert record.to_domain() == sim.order
        assert OrderRecord.model_validate_json(record.model_dump_json()) == record


def test_a_customer_survives_the_trip_through_its_record(small_world: World) -> None:
    for sim in small_world.customers:
        assert CustomerRecord.from_domain(sim.customer).to_domain() == sim.customer


def test_records_write_money_as_cents_and_instants_as_utc(small_world: World) -> None:
    row = json.loads(OrderRecord.from_domain(small_world.orders[0].order).model_dump_json())

    assert isinstance(row["total_cents"], int)
    assert row["placed_at"].endswith("Z")
    assert row["events"][0]["occurred_at"].endswith("Z")


def test_an_instant_with_an_offset_is_stored_as_utc() -> None:
    brasilia = timezone(timedelta(hours=-3))

    record = customer_record(datetime(2026, 5, 1, 9, 0, tzinfo=brasilia))

    assert record.created_at == datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    assert record.created_at.utcoffset() == timedelta(0)


def test_a_naive_instant_is_refused() -> None:
    with pytest.raises(ValidationError, match="naive_datetime"):
        customer_record(datetime(2026, 5, 1))  # noqa: DTZ001


def test_records_refuse_unknown_fields_and_changes() -> None:
    restaurant = RestaurantRecord(id=uuid4(), name="Casa", category="Pizzaria")

    with pytest.raises(ValidationError):
        RestaurantRecord.model_validate({**restaurant.model_dump(), "rating": 5})
    with pytest.raises(ValidationError):
        restaurant.name = "Outra"  # type: ignore[misc]


def test_every_claim_reason_maps_to_an_intent_of_the_same_name_but_for_two() -> None:
    renamed = {
        ClaimReason.DUPLICATE_CHARGE: Intent.BILLING_ISSUE,
        ClaimReason.CUSTOMER_CANCELLATION: Intent.CANCEL_ORDER,
    }

    assert set(INTENT_OF_REASON) == set(ClaimReason)
    for reason, intent in INTENT_OF_REASON.items():
        if reason in renamed:
            assert intent == renamed[reason]
        else:
            assert intent.value == reason.value


def test_there_are_eleven_intents() -> None:
    assert len(Intent) == 11


def test_a_denied_resolution_needs_its_denial_reason() -> None:
    with pytest.raises(ValidationError, match=r"resolution\.denial_reason_mismatch"):
        ResolutionRecord(decision=DecisionKind.DENIED)
    with pytest.raises(ValidationError, match=r"resolution\.denial_reason_mismatch"):
        ResolutionRecord(decision=DecisionKind.ALLOWED, denial_reason=DenialReason.NOT_COVERED)


def test_a_resolution_that_needs_approval_carries_its_reasons() -> None:
    with pytest.raises(ValidationError, match=r"resolution\.approval_reasons_mismatch"):
        ResolutionRecord(decision=DecisionKind.NEEDS_APPROVAL)
    with pytest.raises(ValidationError, match=r"resolution\.approval_reasons_mismatch"):
        ResolutionRecord(
            decision=DecisionKind.ALLOWED,
            approval_reasons=(ApprovalReason.EXCEEDS_ACTION_LIMIT,),
        )
    allowed = ResolutionRecord(decision=DecisionKind.ALLOWED, document=PolicyDocumentId.POL_03)
    assert allowed.approval_reasons == ()


def test_a_personal_data_span_must_not_be_empty() -> None:
    with pytest.raises(ValidationError, match=r"pii_span\.empty"):
        PiiSpan(kind=PiiKind.CPF, start=5, end=5)
    with pytest.raises(ValidationError):
        PiiSpan(kind=PiiKind.CPF, start=-1, end=5)


def test_a_message_row_round_trips() -> None:
    message = MessageRecord(
        id=uuid4(),
        scenario_id=uuid4(),
        split=Split.TRAIN,
        text="Meu CPF é 123",
        intent=Intent.OTHER,
        pii_spans=(PiiSpan(kind=PiiKind.CPF, start=9, end=12),),
        persona="formal",
    )

    assert MessageRecord.model_validate_json(message.model_dump_json()) == message
    assert message.has_injection is False


def test_the_claim_row_has_no_profile_and_no_flipped_label_mark() -> None:
    private = {"profile", "label_flipped", "incident", "exaggerated"}

    assert not private & set(ClaimRecord.model_fields)


def test_a_manifest_starts_without_files_and_round_trips() -> None:
    manifest = Manifest(
        datagen_version="1.0.0",
        seed=42,
        commit="abc1234",
        reference_instant=datetime(2026, 10, 31, 23, 59, 59, tzinfo=UTC),
    )

    assert manifest.files == {}
    assert manifest.dirty is False
    assert manifest.prompt_hashes == {}
    assert Manifest.model_validate_json(manifest.model_dump_json()) == manifest
