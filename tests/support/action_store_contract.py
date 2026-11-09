"""The `ActionStore` contract, run against every implementation (Liskov substitution, tested).

A subclass provides the `harness` fixture: the store plus a way to create what the database needs
before an action can point at an order (the order, the customer and a conversation).
"""

from dataclasses import replace
from datetime import datetime, timedelta
from typing import Protocol

import pytest
from support.action_builders import (
    EXECUTED_AT,
    delivered_order,
    delivered_order_of,
    executed_action,
    refund_of,
    scope_of,
)

from deliverius.actions import ActionStore, ExecutedAction
from deliverius.orders import Order
from deliverius.shared import (
    ConversationId,
    CustomerId,
    CustomerScope,
    SandboxId,
    SandboxScope,
    new_id,
)


class StoreHarness(Protocol):
    store: ActionStore

    async def add_order(self, order: Order) -> ConversationId:
        """Make the order exist (once), with a conversation for it, and return that conversation."""
        ...


class ActionStoreContract:
    @pytest.fixture
    def harness(self) -> StoreHarness:
        raise NotImplementedError

    async def test_inserted_action_is_read_back_by_its_key(self, harness: StoreHarness) -> None:
        order = delivered_order()
        conversation = await harness.add_order(order)
        action = executed_action(order, conversation, refund_of(order.items[0], 800))

        assert await harness.store.insert(action) == action

        found = await harness.store.find_by_key(scope_of(order), action.idempotency_key)
        assert found == action

    async def test_other_customer_or_sandbox_does_not_find_the_action(
        self, harness: StoreHarness
    ) -> None:
        order = delivered_order()
        conversation = await harness.add_order(order)
        action = executed_action(order, conversation, refund_of(order.items[0], 800))
        await harness.store.insert(action)

        neighbour = CustomerScope(order.sandbox_id, CustomerId(new_id()))
        elsewhere = CustomerScope(SandboxId(new_id()), order.customer_id)

        assert await harness.store.find_by_key(neighbour, action.idempotency_key) is None
        assert await harness.store.find_by_key(elsewhere, action.idempotency_key) is None

    async def test_unknown_key_finds_nothing(self, harness: StoreHarness) -> None:
        order = delivered_order()
        await harness.add_order(order)

        assert await harness.store.find_by_key(scope_of(order), "unknown") is None

    async def test_inserting_the_same_key_again_returns_the_stored_action(
        self, harness: StoreHarness
    ) -> None:
        order = delivered_order()
        conversation = await harness.add_order(order)
        proposal = refund_of(order.items[0], 800)
        first = executed_action(order, conversation, proposal)
        second = executed_action(order, conversation, proposal, key=first.idempotency_key)
        await harness.store.insert(first)

        assert await harness.store.insert(second) == first

        assert await harness.store.list_for_order(scope_of(order), order.id) == (first,)

    async def test_history_has_recent_actions_of_the_customer_and_all_of_the_order(
        self, harness: StoreHarness
    ) -> None:
        order = delivered_order()
        other_order = delivered_order_of(order, 9_000)
        since = EXECUTED_AT
        recent_here = await self._store_one(harness, order, since + timedelta(days=1))
        old_here = await self._store_one(harness, order, since - timedelta(days=60), cents=700)
        recent_there = await self._store_one(harness, other_order, since)
        await self._store_one(harness, other_order, since - timedelta(seconds=1), cents=600)

        history = await harness.store.history(scope_of(order), order.id, since)

        assert history.actions == (
            old_here.as_prior(),
            recent_there.as_prior(),
            recent_here.as_prior(),
        )

    async def test_history_leaves_out_other_customers_and_sandboxes(
        self, harness: StoreHarness
    ) -> None:
        order = delivered_order()
        await harness.add_order(order)
        other_customer = replace(delivered_order(), sandbox_id=order.sandbox_id)
        other_sandbox = delivered_order()
        await self._store_one(harness, other_customer, EXECUTED_AT)
        await self._store_one(harness, other_sandbox, EXECUTED_AT)

        history = await harness.store.history(
            CustomerScope(order.sandbox_id, order.customer_id), order.id, EXECUTED_AT
        )

        assert history.actions == ()

    async def test_listing_by_order_follows_the_scope_and_runs_oldest_first(
        self, harness: StoreHarness
    ) -> None:
        order = delivered_order()
        later = await self._store_one(harness, order, EXECUTED_AT + timedelta(hours=1), cents=300)
        earlier = await self._store_one(harness, order, EXECUTED_AT, cents=200)

        owner = await harness.store.list_for_order(scope_of(order), order.id)
        sandbox = await harness.store.list_for_order(SandboxScope(order.sandbox_id), order.id)
        neighbour = CustomerScope(order.sandbox_id, CustomerId(new_id()))
        elsewhere = SandboxScope(SandboxId(new_id()))

        assert owner == (earlier, later)
        assert sandbox == (earlier, later)
        assert await harness.store.list_for_order(neighbour, order.id) == ()
        assert await harness.store.list_for_order(elsewhere, order.id) == ()

    @staticmethod
    async def _store_one(
        harness: StoreHarness, order: Order, executed_at: datetime, *, cents: int = 500
    ) -> ExecutedAction:
        conversation = await harness.add_order(order)
        action = executed_action(
            order, conversation, refund_of(order.items[0], cents, 1), executed_at=executed_at
        )
        await harness.store.insert(action)
        return action
