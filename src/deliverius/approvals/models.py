"""Statuses of the approval, handoff and conversation tables (the database keeps literal copies)."""

from enum import StrEnum


class ConversationStatus(StrEnum):
    OPEN = "open"
    AWAITING_APPROVAL = "awaiting_approval"
    TRANSFERRED = "transferred"
    CLOSED = "closed"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class HandoffStatus(StrEnum):
    OPEN = "open"
    ASSIGNED = "assigned"


class Priority(StrEnum):
    NORMAL = "normal"
    HIGH = "high"
