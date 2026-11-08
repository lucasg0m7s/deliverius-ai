from datetime import UTC, datetime
from typing import Any

import pytest

from deliverius.shared.audit import AuditActorType, AuditEntry, JsonValue
from deliverius.shared.errors import InvariantError
from deliverius.shared.ids import SandboxId, new_id

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def make_entry(**overrides: Any) -> AuditEntry:
    fields: dict[str, Any] = {
        "sandbox_id": SandboxId(new_id()),
        "actor_type": AuditActorType.REVIEWER,
        "actor_id": new_id(),
        "action": "approvals.decided",
        "target_type": "approval_request",
        "target_id": new_id(),
        "occurred_at": NOW,
        "data": {"decision": "approved", "amount_cents": 4590},
    }
    return AuditEntry(**(fields | overrides))


def test_valid_entry_keeps_every_field_and_gets_a_v7_id() -> None:
    entry = make_entry()

    assert entry.action == "approvals.decided"
    assert entry.data == {"decision": "approved", "amount_cents": 4590}
    assert entry.id.version == 7


def test_system_actor_may_have_no_id() -> None:
    assert make_entry(actor_type=AuditActorType.SYSTEM, actor_id=None).actor_id is None


@pytest.mark.parametrize(
    "data",
    [
        {"cpf": "123.456.789-09"},
        {"Email": "a@b.c"},
        {"customer": {"name": "Ana"}},
        {"changes": [{"field": "x"}, {"address": "Rua A, 1"}]},
        {"outer": {"inner": [{"deep": {"phone": "11999999999"}}]}},
    ],
)
def test_personal_data_keys_are_rejected_even_nested(data: dict[str, JsonValue]) -> None:
    with pytest.raises(InvariantError, match=r"audit\.personal_data"):
        make_entry(data=data)


@pytest.mark.parametrize("action", ["approved", "Approvals.decided", "approvals.", "a.b.c", ""])
def test_action_must_be_module_dot_event(action: str) -> None:
    with pytest.raises(InvariantError, match=r"audit\.action_format"):
        make_entry(action=action)


def test_naive_instant_is_rejected() -> None:
    with pytest.raises(InvariantError, match=r"naive_datetime\.occurred_at"):
        make_entry(occurred_at=datetime(2026, 11, 1, 12, 0))  # noqa: DTZ001
