"""The fixed sample of the LLM baseline: the daily quota cannot pay for every test message.

Approach 4 runs on the whole human test and on `per_intent` messages of each intent from each
synthetic set (design D5). The draw depends only on the seed, the set and the intent, so the same
ids come out on every run; approaches 1 to 3 are measured on the same sample for the comparison.
"""

import hashlib
from collections.abc import Iterable

import numpy as np

from deliverius.datagen.records import Intent
from research.intent.data import MessageSet


def keyed_rng(seed: int, *keys: str) -> np.random.Generator:
    """A generator for one draw, independent of the order the draws are made in."""
    material = ":".join((str(seed), *keys)).encode()
    return np.random.default_rng(int.from_bytes(hashlib.sha256(material).digest()[:8]))


def stratified_ids(messages: MessageSet, per_intent: int, seed: int) -> tuple[str, ...]:
    """`per_intent` ids of each intent (all of them when there are fewer), sorted."""
    chosen: list[str] = []
    for intent in Intent:
        ids = sorted(e.id for e in messages.examples if e.intent is intent)
        rng = keyed_rng(seed, messages.corpus, intent)
        rows = rng.choice(len(ids), size=min(per_intent, len(ids)), replace=False)
        chosen += [ids[row] for row in rows]
    return tuple(sorted(chosen))


def llm_sample(
    synthetic: Iterable[MessageSet], human: MessageSet | None, per_intent: int, seed: int
) -> dict[str, tuple[str, ...]]:
    """The sample ids per set: stratified for the synthetic sets, everything for the human one."""
    sample = {
        str(messages.corpus): stratified_ids(messages, per_intent, seed) for messages in synthetic
    }
    if human is not None:
        sample[str(human.corpus)] = human.ids
    return sample
