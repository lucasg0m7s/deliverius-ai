import hashlib
from dataclasses import replace
from uuid import UUID

import pytest

from deliverius.policy import ActionKind, ClaimedItem, ClaimReason, ProposedAction, proposal_key
from deliverius.shared import ConversationId, Money, OrderId, OrderItemId

CONVERSATION = ConversationId(UUID("01930000-0000-7000-8000-000000000001"))
ORDER = OrderId(UUID("01930000-0000-7000-8000-000000000002"))
ITEM_A = OrderItemId(UUID("01930000-0000-7000-8000-00000000000a"))
ITEM_B = OrderItemId(UUID("01930000-0000-7000-8000-00000000000b"))

# Fixed on purpose: a change here is a change of the key format, which needs a new `v`.
GOLDEN_DIGEST = "5fbcfdc93ca2c9c99be49927f5dc41463c7db8ae54b86b1160110886d8f340bf"


def proposal(*items: ClaimedItem) -> ProposedAction:
    return ProposedAction(
        kind=ActionKind.REFUND,
        reason=ClaimReason.ITEM_MISSING,
        amount=Money(800),
        items=items,
    )


BASE = proposal(ClaimedItem(ITEM_A, 1), ClaimedItem(ITEM_B, 2))


def test_key_is_the_sha256_of_the_canonical_json() -> None:
    canonical = (
        '{"amount_cents":800,"conversation":"01930000-0000-7000-8000-000000000001",'
        '"items":[["01930000-0000-7000-8000-00000000000a",1],'
        '["01930000-0000-7000-8000-00000000000b",2]],"kind":"refund",'
        '"order":"01930000-0000-7000-8000-000000000002","reason":"item_missing","v":1}'
    )

    assert proposal_key(CONVERSATION, ORDER, BASE) == hashlib.sha256(canonical.encode()).hexdigest()


def test_key_of_a_known_proposal_is_fixed() -> None:
    assert proposal_key(CONVERSATION, ORDER, BASE) == GOLDEN_DIGEST


def test_items_in_another_order_give_the_same_key() -> None:
    reversed_items = proposal(ClaimedItem(ITEM_B, 2), ClaimedItem(ITEM_A, 1))

    assert proposal_key(CONVERSATION, ORDER, reversed_items) == GOLDEN_DIGEST


def test_repeated_items_are_summed_before_the_key_is_made() -> None:
    split = proposal(ClaimedItem(ITEM_B, 1), ClaimedItem(ITEM_A, 1), ClaimedItem(ITEM_B, 1))

    assert proposal_key(CONVERSATION, ORDER, split) == GOLDEN_DIGEST


def test_different_quantity_or_item_gives_another_key() -> None:
    more = proposal(ClaimedItem(ITEM_A, 2), ClaimedItem(ITEM_B, 2))
    fewer_items = proposal(ClaimedItem(ITEM_A, 1))

    keys = {proposal_key(CONVERSATION, ORDER, candidate) for candidate in (BASE, more, fewer_items)}

    assert len(keys) == 3


@pytest.mark.parametrize(
    "changed",
    [
        replace(BASE, amount=Money(801)),
        replace(BASE, kind=ActionKind.COUPON),
        replace(BASE, reason=ClaimReason.WRONG_ITEM),
    ],
    ids=["amount", "kind", "reason"],
)
def test_different_proposal_content_gives_another_key(changed: ProposedAction) -> None:
    assert proposal_key(CONVERSATION, ORDER, changed) != GOLDEN_DIGEST


def test_different_order_gives_another_key() -> None:
    other_order = OrderId(UUID("01930000-0000-7000-8000-000000000003"))

    assert proposal_key(CONVERSATION, other_order, BASE) != GOLDEN_DIGEST


def test_different_conversation_gives_another_key() -> None:
    other_conversation = ConversationId(UUID("01930000-0000-7000-8000-000000000004"))

    assert proposal_key(other_conversation, ORDER, BASE) != GOLDEN_DIGEST


def test_proposal_without_items_has_a_key() -> None:
    cancellation = ProposedAction(
        ActionKind.CANCELLATION, ClaimReason.CUSTOMER_CANCELLATION, Money(5_000)
    )

    key = proposal_key(CONVERSATION, ORDER, cancellation)

    assert len(key) == 64
    assert key != GOLDEN_DIGEST
