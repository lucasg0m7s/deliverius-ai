from typing import NewType
from uuid import UUID

from uuid_utils.compat import uuid7

SandboxId = NewType("SandboxId", UUID)
CustomerId = NewType("CustomerId", UUID)
RestaurantId = NewType("RestaurantId", UUID)
OrderId = NewType("OrderId", UUID)
OrderItemId = NewType("OrderItemId", UUID)
DeliveryEventId = NewType("DeliveryEventId", UUID)
AuditEntryId = NewType("AuditEntryId", UUID)
ConversationId = NewType("ConversationId", UUID)
ActionId = NewType("ActionId", UUID)
ApprovalRequestId = NewType("ApprovalRequestId", UUID)
HandoffId = NewType("HandoffId", UUID)
ReviewerId = NewType("ReviewerId", UUID)


def new_id() -> UUID:
    """The only ID generator: UUID v7, ordered by creation time.

    Wrap the result in the entity's type, e.g. `OrderId(new_id())`.
    On Python 3.14 this becomes `uuid.uuid7()`.
    """
    return uuid7()
