"""The key of a proposal: one value for the action's idempotency and the approval's identity."""

import hashlib
import json
from collections import Counter
from typing import Final

from deliverius.policy.models import ProposedAction
from deliverius.shared.ids import ConversationId, OrderId

# Bump when the canonical form changes, so new keys never collide with stored ones.
KEY_VERSION: Final = 1


def proposal_key(conversation_id: ConversationId, order_id: OrderId, action: ProposedAction) -> str:
    """SHA-256, in hex, of the canonical JSON of the proposal in its conversation.

    Repeated items are summed and then sorted by ID, so the same claim written in another order, or
    split in two lines, yields the same key.
    """
    quantities: Counter[str] = Counter()
    for item in action.items:
        quantities[str(item.order_item_id)] += item.quantity
    canonical = {
        "v": KEY_VERSION,
        "conversation": str(conversation_id),
        "order": str(order_id),
        "kind": action.kind.value,
        "reason": action.reason.value,
        "amount_cents": action.amount.cents,
        "items": [[item_id, quantities[item_id]] for item_id in sorted(quantities)],
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
