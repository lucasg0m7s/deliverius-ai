"""A scorer with a fixed probability: for unit tests, and for the agent graph before a model."""

from deliverius.fraud.decision import needs_review
from deliverius.fraud.models import ClaimContext, FraudFactor, FraudScore
from deliverius.shared.money import Money


class FixedFraudScorer:
    """Answers the same probability for every claim; the review rule is the real one."""

    def __init__(
        self,
        probability: float,
        *,
        review_cost: Money = Money(400),
        factors: tuple[FraudFactor, ...] = (),
        model_version: str = "fixed",
    ) -> None:
        self._probability = probability
        self._review_cost = review_cost
        self._factors = factors
        self._model_version = model_version
        self.scored: list[ClaimContext] = []

    @property
    def model_version(self) -> str:
        return self._model_version

    async def score(self, context: ClaimContext) -> FraudScore:
        self.scored.append(context)
        return FraudScore(
            probability=self._probability,
            model_version=self._model_version,
            needs_review=needs_review(self._probability, context.requested, self._review_cost),
            factors=self._factors,
        )
