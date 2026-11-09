"""The customer's history around a claim, read from Postgres the way the training reads the dataset.

A prior claim is an approval request (it went to review, whatever the reviewer decided) or an action
executed without one (automatic). An action that came from an approval is not counted again. The
reviewer's decision is never read: it would leak the label. Three queries, each on its own index,
each scoped to the customer and the sandbox in the `WHERE`.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Final

from sqlalchemy import Integer, Row, cast, false, literal_column, select, true, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from deliverius.db.errors import translate_db_errors
from deliverius.db.tables import ActionRow, ApprovalRequestRow, CustomerRow, OrderRow
from deliverius.fraud.features import LONG_WINDOW, MAX_ROWS_PER_WINDOW
from deliverius.fraud.models import CustomerClaimHistory, CustomerFacts, PriorClaim
from deliverius.policy import ActionKind
from deliverius.shared.access import CustomerScope
from deliverius.shared.clock import require_aware
from deliverius.shared.errors import NotFoundError
from deliverius.shared.money import Money

_AT: Final = "at"


class PostgresFraudContextReader:
    """`FraudContextReader` on Postgres. Uses the caller's session and never commits."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def history(self, scope: CustomerScope, claimed_at: datetime) -> CustomerClaimHistory:
        require_aware(claimed_at, "claimed_at")
        start = claimed_at - LONG_WINDOW
        with translate_db_errors():
            created_at = await self._account_created_at(scope)
            orders = await self._order_instants(scope, start, claimed_at)
            claims = await self._claims(scope, start, claimed_at)
        return CustomerClaimHistory(
            customer=CustomerFacts(created_at),
            order_placed_at=orders,
            claims=tuple(_to_prior_claim(row) for row in claims),
        )

    async def _account_created_at(self, scope: CustomerScope) -> datetime:
        statement = select(CustomerRow.created_at).where(
            CustomerRow.id == scope.customer_id, CustomerRow.sandbox_id == scope.sandbox_id
        )
        created_at = (await self._session.execute(statement)).scalar_one_or_none()
        if created_at is None:
            raise NotFoundError("customer")
        return created_at

    async def _order_instants(
        self, scope: CustomerScope, start: datetime, claimed_at: datetime
    ) -> tuple[datetime, ...]:
        statement = (
            select(OrderRow.placed_at)
            .where(
                OrderRow.sandbox_id == scope.sandbox_id,
                OrderRow.customer_id == scope.customer_id,
                OrderRow.placed_at >= start,
                OrderRow.placed_at < claimed_at,
            )
            .order_by(OrderRow.placed_at.desc())
            .limit(MAX_ROWS_PER_WINDOW)
        )
        return tuple((await self._session.execute(statement)).scalars().all())

    async def _claims(
        self, scope: CustomerScope, start: datetime, claimed_at: datetime
    ) -> Sequence[Row[Any]]:
        requested = ApprovalRequestRow.proposal["amount_cents"].astext
        reviewed = select(
            ApprovalRequestRow.claimed_at.label(_AT),
            ApprovalRequestRow.proposal["kind"].astext.label("kind"),
            cast(requested, Integer).label("amount_cents"),
            true().label("reviewed"),
        ).where(
            ApprovalRequestRow.sandbox_id == scope.sandbox_id,
            ApprovalRequestRow.customer_id == scope.customer_id,
            ApprovalRequestRow.claimed_at >= start,
            ApprovalRequestRow.claimed_at < claimed_at,
        )
        # An automatic action is executed at the moment of the claim; one from an approval is
        # already counted by its approval request.
        automatic = select(
            ActionRow.executed_at.label(_AT),
            ActionRow.kind,
            ActionRow.amount_cents,
            false().label("reviewed"),
        ).where(
            ActionRow.sandbox_id == scope.sandbox_id,
            ActionRow.customer_id == scope.customer_id,
            ActionRow.approval_request_id.is_(None),
            ActionRow.executed_at >= start,
            ActionRow.executed_at < claimed_at,
        )
        statement = (
            union_all(reviewed, automatic)
            .order_by(literal_column(_AT).desc())
            .limit(MAX_ROWS_PER_WINDOW)
        )
        return (await self._session.execute(statement)).all()


def _to_prior_claim(row: Row[Any]) -> PriorClaim:
    return PriorClaim(
        at=row.at,
        kind=ActionKind(row.kind),
        requested=Money(row.amount_cents),
        reviewed=row.reviewed,
    )
