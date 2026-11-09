"""Refund fraud: a calibrated score for a claim, from features shared with the training code.

The score runs only on claims the policy did not deny, and never makes allowed what the policy
sent to a person: it only adds a reason to review.
"""

from deliverius.fraud.context import claim_context
from deliverius.fraud.decision import needs_review, top_factors
from deliverius.fraud.errors import FraudModelError, FraudScoringError
from deliverius.fraud.fake import FixedFraudScorer
from deliverius.fraud.features import (
    FEATURE_NAMES,
    FEATURE_SCHEMA,
    FEATURES_VERSION,
    MAX_ROWS_PER_WINDOW,
    compute_features,
    schema_hash,
)
from deliverius.fraud.model_files import fetch_fraud_model, load_fraud_model
from deliverius.fraud.models import (
    ClaimContext,
    CustomerClaimHistory,
    CustomerFacts,
    FraudFactor,
    FraudScore,
    PriorClaim,
)
from deliverius.fraud.ports import FraudContextReader, FraudScorer
from deliverius.fraud.scorer import FraudModel, LogisticFraudScorer

__all__ = [
    "FEATURES_VERSION",
    "FEATURE_NAMES",
    "FEATURE_SCHEMA",
    "MAX_ROWS_PER_WINDOW",
    "ClaimContext",
    "CustomerClaimHistory",
    "CustomerFacts",
    "FixedFraudScorer",
    "FraudContextReader",
    "FraudFactor",
    "FraudModel",
    "FraudModelError",
    "FraudScore",
    "FraudScorer",
    "FraudScoringError",
    "LogisticFraudScorer",
    "PriorClaim",
    "claim_context",
    "compute_features",
    "fetch_fraud_model",
    "load_fraud_model",
    "needs_review",
    "schema_hash",
    "top_factors",
]
