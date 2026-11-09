"""The database keeps literal copies of the enums (it cannot import them); these must not drift."""

from enum import StrEnum

import pytest

from deliverius.approvals import ApprovalStatus, ConversationStatus, HandoffStatus, Priority
from deliverius.db import tables
from deliverius.policy import ActionKind, ApprovedBy, ClaimReason


@pytest.mark.parametrize(
    ("literal", "enum"),
    [
        (tables.ACTION_KINDS, ActionKind),
        (tables.CLAIM_REASONS, ClaimReason),
        (tables.APPROVED_BY, ApprovedBy),
        (tables.CONVERSATION_STATUSES, ConversationStatus),
        (tables.APPROVAL_STATUSES, ApprovalStatus),
        (tables.HANDOFF_STATUSES, HandoffStatus),
        (tables.HANDOFF_PRIORITIES, Priority),
    ],
    ids=lambda value: "" if isinstance(value, tuple) else value.__name__,
)
def test_check_literals_equal_the_enum_values(
    literal: tuple[str, ...], enum: type[StrEnum]
) -> None:
    assert literal == tuple(member.value for member in enum)
