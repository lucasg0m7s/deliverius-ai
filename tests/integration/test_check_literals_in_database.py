"""The CHECK lists written in migration 0005 equal the enums the code uses."""

import re
from enum import StrEnum

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from deliverius.approvals import ApprovalStatus, ConversationStatus, HandoffStatus, Priority
from deliverius.policy import ActionKind, ApprovedBy, ClaimReason

pytestmark = pytest.mark.integration

QUOTED_VALUE = re.compile(r"'([^']+)'::text")


@pytest.mark.parametrize(
    ("table", "constraint", "enum"),
    [
        ("actions", "ck_actions_kind_known", ActionKind),
        ("actions", "ck_actions_reason_known", ClaimReason),
        ("actions", "ck_actions_approved_by_known", ApprovedBy),
        ("conversations", "ck_conversations_status_known", ConversationStatus),
        ("approval_requests", "ck_approval_requests_status_known", ApprovalStatus),
        ("handoffs", "ck_handoffs_status_known", HandoffStatus),
        ("handoffs", "ck_handoffs_priority_known", Priority),
    ],
)
async def test_check_constraint_lists_the_enum_values(
    session: AsyncSession, table: str, constraint: str, enum: type[StrEnum]
) -> None:
    definition = await session.scalar(
        text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = :name AND conrelid = CAST(:table AS regclass)"
        ),
        {"name": constraint, "table": table},
    )

    assert definition is not None
    listed = QUOTED_VALUE.findall(definition)
    assert sorted(listed) == sorted(member.value for member in enum)
