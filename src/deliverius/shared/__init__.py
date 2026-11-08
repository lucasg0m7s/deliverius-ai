"""Basic domain vocabulary: money, IDs, time, errors, access scope, audit entry.

Pure: no database, web framework, LLM or logging library.
"""

from deliverius.shared.access import AccessScope, CustomerScope, SandboxScope
from deliverius.shared.audit import AuditActorType, AuditEntry, JsonValue
from deliverius.shared.clock import Clock, FixedClock, SystemClock, require_aware
from deliverius.shared.errors import (
    ConflictError,
    DeliveriusError,
    ExternalServiceError,
    InvalidInputError,
    InvariantError,
    NotFoundError,
)
from deliverius.shared.ids import (
    AuditEntryId,
    CustomerId,
    DeliveryEventId,
    OrderId,
    OrderItemId,
    RestaurantId,
    SandboxId,
    new_id,
)
from deliverius.shared.money import Money

__all__ = [
    "AccessScope",
    "AuditActorType",
    "AuditEntry",
    "AuditEntryId",
    "Clock",
    "ConflictError",
    "CustomerId",
    "CustomerScope",
    "DeliveriusError",
    "DeliveryEventId",
    "ExternalServiceError",
    "FixedClock",
    "InvalidInputError",
    "InvariantError",
    "JsonValue",
    "Money",
    "NotFoundError",
    "OrderId",
    "OrderItemId",
    "RestaurantId",
    "SandboxId",
    "SandboxScope",
    "SystemClock",
    "new_id",
    "require_aware",
]
