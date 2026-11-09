"""Policy: decides, in code, whether a refund, coupon or cancellation is allowed.

Pure: depends only on `orders` and `shared`; no database, web framework, LLM or logging library.
"""

from deliverius.policy.documents import (
    PolicyDocument,
    PolicyDocumentError,
    format_limit,
    load_policy_documents,
    render_policy_document,
)
from deliverius.policy.engine import evaluate
from deliverius.policy.keys import proposal_key
from deliverius.policy.limits import POLICY_LIMITS, PolicyLimits
from deliverius.policy.models import (
    ActionKind,
    Allowed,
    ApprovalReason,
    ApprovedBy,
    ClaimedItem,
    ClaimReason,
    CustomerHistory,
    DenialReason,
    Denied,
    NeedsApproval,
    PolicyDecision,
    PolicyDocumentId,
    PriorAction,
    ProposedAction,
)

__all__ = [
    "POLICY_LIMITS",
    "ActionKind",
    "Allowed",
    "ApprovalReason",
    "ApprovedBy",
    "ClaimReason",
    "ClaimedItem",
    "CustomerHistory",
    "DenialReason",
    "Denied",
    "NeedsApproval",
    "PolicyDecision",
    "PolicyDocument",
    "PolicyDocumentError",
    "PolicyDocumentId",
    "PolicyLimits",
    "PriorAction",
    "ProposedAction",
    "evaluate",
    "format_limit",
    "load_policy_documents",
    "proposal_key",
    "render_policy_document",
]
