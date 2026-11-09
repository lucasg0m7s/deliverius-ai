from datetime import datetime
from typing import Protocol

from deliverius.fraud.models import ClaimContext, CustomerClaimHistory, FraudScore
from deliverius.shared.access import CustomerScope


class FraudScorer(Protocol):
    """Scores a claim the policy did not deny. Raises `FraudScoringError` when it cannot."""

    @property
    def model_version(self) -> str: ...

    async def score(self, context: ClaimContext) -> FraudScore: ...


class FraudContextReader(Protocol):
    """The customer's account, orders and earlier claims around `claimed_at`, inside `scope`.

    Contract: every order placed and every claim made in the 90 days before `claimed_at`, newest
    first, at most `MAX_ROWS_PER_WINDOW` of each. Later facts may come along: features drop them.
    Raises `NotFoundError` when the customer is not in the scope.
    """

    async def history(self, scope: CustomerScope, claimed_at: datetime) -> CustomerClaimHistory: ...
