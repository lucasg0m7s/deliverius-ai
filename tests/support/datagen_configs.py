"""Generator configurations shared by the datagen tests."""

from deliverius.datagen import DatagenConfig
from deliverius.datagen.config import ClaimRates, MessageConfig, ProfileMix

# Plenty of every profile, so each kind of claim shows up at least a few times.
MIXED_CONFIG = DatagenConfig(
    seed=11,
    customer_count=300,
    restaurant_count=12,
    order_count=4_000,
    profile_mix=ProfileMix(honest=0.4, opportunist=0.3, fraudster=0.3),
    claim_rates=ClaimRates(unlucky_honest_share=0.2),
)
SMALL_CONFIG = DatagenConfig(seed=7, customer_count=50, restaurant_count=8, order_count=300)

# Small message sets, so the tests that call the (scripted) LLM stay fast: 2 scenarios per intent.
MESSAGES_CONFIG = DatagenConfig(
    seed=21,
    restaurant_count=10,
    messages=MessageConfig(scenarios_per_intent=2, holdout_scenarios_per_intent=1),
)

# The command line end to end on the scripted LLM: a small world, but 3 scenarios per intent, which
# is the least the 70/15/15 split needs to give every intent all three sides.
CLI_CONFIG = DatagenConfig(
    customer_count=60,
    restaurant_count=8,
    order_count=400,
    messages=MessageConfig(scenarios_per_intent=3, holdout_scenarios_per_intent=2),
)
