from support.fraud_builders import claim_ctx

from deliverius.fraud.fake import FixedFraudScorer
from deliverius.fraud.models import FraudFactor
from deliverius.shared.money import Money


async def test_the_fixed_scorer_answers_its_probability_and_records_the_claim() -> None:
    factors = (FraudFactor("claims_30d", 1.0),)
    scorer = FixedFraudScorer(0.3, factors=factors, model_version="test")
    context = claim_ctx(requested_cents=18_000)

    score = await scorer.score(context)

    assert score.probability == 0.3
    assert score.factors == factors
    assert score.model_version == scorer.model_version == "test"
    assert scorer.scored == [context]


async def test_the_fixed_scorer_applies_the_real_review_rule() -> None:
    scorer = FixedFraudScorer(0.3, review_cost=Money(400))

    small = await scorer.score(claim_ctx(requested_cents=800))
    large = await scorer.score(claim_ctx(requested_cents=18_000))

    assert (small.needs_review, large.needs_review) == (False, True)
