import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from deliverius.shared.clock import require_aware
from deliverius.shared.errors import InvariantError
from deliverius.shared.ids import AuditEntryId, SandboxId, new_id

type JsonValue = str | int | float | bool | list[JsonValue] | dict[str, JsonValue] | None

ACTION_PATTERN = re.compile(r"[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*")

# Audit rows are kept for years and are never updated, so they may only point at people by ID.
PERSONAL_DATA_KEYS = frozenset({"name", "cpf", "phone", "email", "address"})


class AuditActorType(StrEnum):
    CUSTOMER = "customer"
    REVIEWER = "reviewer"
    AGENT = "agent"
    SYSTEM = "system"


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """Who did what to which object, when; validated before it can reach the database."""

    sandbox_id: SandboxId
    actor_type: AuditActorType
    actor_id: UUID | None
    action: str
    target_type: str
    target_id: UUID
    occurred_at: datetime
    data: Mapping[str, JsonValue] = field(default_factory=dict)
    id: AuditEntryId = field(default_factory=lambda: AuditEntryId(new_id()))

    def __post_init__(self) -> None:
        if not ACTION_PATTERN.fullmatch(self.action):
            raise InvariantError("audit.action_format")
        require_aware(self.occurred_at, "occurred_at")
        found = _personal_data_path(self.data, "data")
        if found is not None:
            raise InvariantError(f"audit.personal_data:{found}")


class AuditSink(Protocol):
    """Where an audit entry goes; it joins the caller's transaction, saved with it or not at all."""

    async def record(self, entry: AuditEntry) -> None: ...


def _personal_data_path(value: JsonValue | Mapping[str, JsonValue], path: str) -> str | None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key.lower() in PERSONAL_DATA_KEYS:
                return f"{path}.{key}"
            found = _personal_data_path(item, f"{path}.{key}")
            if found is not None:
                return found
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found = _personal_data_path(item, f"{path}[{index}]")
            if found is not None:
                return found
    return None
