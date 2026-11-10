"""The LLM baseline sample: the same ids on every run, stratified by intent."""

from collections import Counter

from support.intent_data import message_set

from deliverius.datagen.records import Intent
from research.intent.data import Corpus
from research.intent.sample import llm_sample, stratified_ids


def test_the_draw_is_the_same_for_the_same_seed_and_changes_with_it() -> None:
    test = message_set(Corpus.TEST, 40)

    first = stratified_ids(test, 20, seed=1)

    assert first == stratified_ids(test, 20, seed=1)
    assert first != stratified_ids(test, 20, seed=2)


def test_each_intent_gets_its_share_or_all_it_has() -> None:
    test = message_set(Corpus.TEST, 40)
    few = message_set(Corpus.HOLDOUT, 5, intents=[Intent.SENSITIVE])

    counts = Counter(i.split("-")[1] for i in stratified_ids(test, 20, seed=1))

    assert set(counts.values()) == {20}
    assert len(counts) == 11
    assert len(stratified_ids(few, 20, seed=1)) == 5


def test_the_sample_takes_the_whole_human_test() -> None:
    human = message_set(Corpus.HUMAN, 3)

    sample = llm_sample(
        [message_set(Corpus.TEST, 30), message_set(Corpus.HOLDOUT, 30)], human, 20, seed=1
    )

    assert {name: len(ids) for name, ids in sample.items()} == {
        "test": 220,
        "holdout": 220,
        "human": 33,
    }
    assert sample["human"] == human.ids


def test_without_the_human_test_only_the_synthetic_sets_are_sampled() -> None:
    assert set(llm_sample([message_set(Corpus.VALIDATION, 25)], None, 20, seed=1)) == {"validation"}
