"""Basic domain vocabulary: money, IDs, time, errors, access scope, audit entry.

Pure: no database, web framework, LLM or logging library.
"""

from deliverius.shared.access import AccessScope, CustomerScope, SandboxScope, is_visible
from deliverius.shared.audit import (
    AuditActorType,
    AuditEntry,
    AuditSink,
    InMemoryAuditSink,
    JsonValue,
)
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
    ActionId,
    ApprovalRequestId,
    AuditEntryId,
    ConversationId,
    CustomerId,
    DeliveryEventId,
    HandoffId,
    OrderId,
    OrderItemId,
    RestaurantId,
    ReviewerId,
    SandboxId,
    new_id,
)
from deliverius.shared.money import Money

__all__ = [
    "AccessScope",
    "ActionId",
    "ApprovalRequestId",
    "AuditActorType",
    "AuditEntry",
    "AuditEntryId",
    "AuditSink",
    "Clock",
    "ConflictError",
    "ConversationId",
    "CustomerId",
    "CustomerScope",
    "DeliveriusError",
    "DeliveryEventId",
    "ExternalServiceError",
    "FixedClock",
    "HandoffId",
    "InMemoryAuditSink",
    "InvalidInputError",
    "InvariantError",
    "JsonValue",
    "Money",
    "NotFoundError",
    "OrderId",
    "OrderItemId",
    "RestaurantId",
    "ReviewerId",
    "SandboxId",
    "SandboxScope",
    "SystemClock",
    "is_visible",
    "new_id",
    "require_aware",
]
