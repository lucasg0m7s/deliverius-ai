from datetime import datetime

from deliverius.fraud.features import LONG_WINDOW, MAX_ROWS_PER_WINDOW
from deliverius.fraud.models import CustomerClaimHistory
from deliverius.shared.access import CustomerScope
from deliverius.shared.clock import require_aware
from deliverius.shared.errors import NotFoundError


class InMemoryFraudContextReader:
    """`FraudContextReader` over histories in a dict, with the window and ceiling of Postgres."""

    def __init__(self) -> None:
        self._histories: dict[CustomerScope, CustomerClaimHistory] = {}

    def add(self, scope: CustomerScope, history: CustomerClaimHistory) -> None:
        self._histories[scope] = history

    async def history(self, scope: CustomerScope, claimed_at: datetime) -> CustomerClaimHistory:
        require_aware(claimed_at, "claimed_at")
        stored = self._histories.get(scope)
        if stored is None:
            raise NotFoundError("customer")
        start = claimed_at - LONG_WINDOW
        orders = sorted(
            (at for at in stored.order_placed_at if start <= at < claimed_at), reverse=True
        )
        claims = sorted(
            (claim for claim in stored.claims if start <= claim.at < claimed_at),
            key=lambda claim: claim.at,
            reverse=True,
        )
        return CustomerClaimHistory(
            customer=stored.customer,
            order_placed_at=tuple(orders[:MAX_ROWS_PER_WINDOW]),
            claims=tuple(claims[:MAX_ROWS_PER_WINDOW]),
        )
