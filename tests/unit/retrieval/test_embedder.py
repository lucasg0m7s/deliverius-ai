from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from tokenizers import Tokenizer, models, pre_tokenizers

from deliverius.retrieval.embedder import (
    BATCH_SIZE,
    EMBEDDING_DIMENSIONS,
    MAX_TOKENS,
    E5Embedder,
)
from deliverius.retrieval.model_files import ModelFileError

PAD, UNKNOWN, QUERY, PASSAGE, A, B, C = range(7)
VOCABULARY = {
    "<pad>": PAD,
    "[UNK]": UNKNOWN,
    "query:": QUERY,
    "passage:": PASSAGE,
    "a": A,
    "b": B,
    "c": C,
}
GARBAGE = 999.0
ALL_INPUTS = ("input_ids", "attention_mask", "token_type_ids")


@dataclass(frozen=True)
class NamedInput:
    name: str


class FakeSession:
    """Hidden state of a token is `[id, 1, 0, 0]`; padding positions hold garbage on purpose."""

    def __init__(self, input_names: Sequence[str] = ALL_INPUTS) -> None:
        self._input_names = input_names
        self.feeds: list[Mapping[str, Any]] = []

    def get_inputs(self) -> Sequence[NamedInput]:
        return [NamedInput(name) for name in self._input_names]

    def run(self, output_names: list[str] | None, input_feed: Mapping[str, Any]) -> list[Any]:
        self.feeds.append(input_feed)
        ids = input_feed["input_ids"]
        hidden = np.zeros((*ids.shape, 4), dtype=np.float32)
        hidden[:, :, 0] = ids
        hidden[:, :, 1] = 1.0
        hidden[ids == PAD] = GARBAGE
        return [hidden]


def make_tokenizer(with_pad: bool = True) -> Tokenizer:
    vocabulary = VOCABULARY if with_pad else {k: v for k, v in VOCABULARY.items() if k != "<pad>"}
    tokenizer = Tokenizer(models.WordLevel(vocabulary, unk_token="[UNK]"))  # noqa: S106 - a tokenizer token
    tokenizer.pre_tokenizer = pre_tokenizers.WhitespaceSplit()
    return tokenizer


def make_embedder(session: FakeSession | None = None) -> tuple[E5Embedder, FakeSession]:
    session = session or FakeSession()
    return E5Embedder(session, make_tokenizer()), session


def unit(*values: float) -> np.ndarray:
    vector = np.array(values, dtype=np.float32)
    return vector / np.linalg.norm(vector)


def test_a_query_gets_the_query_prefix() -> None:
    embedder, session = make_embedder()

    embedder.embed_queries(["a b"])

    assert session.feeds[0]["input_ids"].tolist() == [[QUERY, A, B]]


def test_a_passage_gets_the_passage_prefix() -> None:
    embedder, session = make_embedder()

    embedder.embed_passages(["a b"])

    assert session.feeds[0]["input_ids"].tolist() == [[PASSAGE, A, B]]


def test_the_vector_is_the_masked_mean_normalised() -> None:
    embedder, _ = make_embedder()

    vectors = embedder.embed_queries(["a b"])

    expected = unit((QUERY + A + B) / 3, 1, 0, 0)
    assert vectors.dtype == np.float32
    assert vectors.shape == (1, 4)
    np.testing.assert_allclose(vectors[0], expected, atol=1e-6)


def test_padding_of_a_longer_text_in_the_batch_does_not_change_a_vector() -> None:
    embedder, session = make_embedder()

    alone = embedder.embed_queries(["a"])
    together = embedder.embed_queries(["a", "a b c a b c"])

    assert session.feeds[1]["attention_mask"].tolist()[0] == [1, 1, 0, 0, 0, 0, 0]
    np.testing.assert_allclose(together[0], alone[0], atol=1e-6)


def test_every_vector_has_norm_one() -> None:
    embedder, _ = make_embedder()

    vectors = embedder.embed_passages(["a", "b c", "c c c a"])

    np.testing.assert_allclose(np.linalg.norm(vectors, axis=1), 1.0, atol=1e-5)


def test_a_text_longer_than_the_limit_is_truncated() -> None:
    embedder, session = make_embedder()

    embedder.embed_queries([" ".join(["a"] * (MAX_TOKENS * 2))])

    assert session.feeds[0]["input_ids"].shape == (1, MAX_TOKENS)


def test_a_large_list_is_split_in_batches_and_keeps_the_order() -> None:
    embedder, session = make_embedder()
    texts = [" ".join(["a"] * (index % 3 + 1)) for index in range(BATCH_SIZE * 2 + 5)]

    vectors = embedder.embed_queries(texts)

    assert len(session.feeds) == 3
    assert vectors.shape == (len(texts), 4)
    for index, text in enumerate(texts):
        single, _ = make_embedder()
        np.testing.assert_allclose(vectors[index], single.embed_queries([text])[0], atol=1e-6)


def test_no_text_gives_an_empty_matrix_without_running_the_model() -> None:
    embedder, session = make_embedder()

    vectors = embedder.embed_queries([])

    assert vectors.shape == (0, EMBEDDING_DIMENSIONS)
    assert vectors.dtype == np.float32
    assert session.feeds == []


def test_the_embedder_sends_only_the_inputs_the_model_declares() -> None:
    embedder, session = make_embedder(FakeSession(("input_ids", "attention_mask")))

    embedder.embed_queries(["a"])

    assert set(session.feeds[0]) == {"input_ids", "attention_mask"}


def test_token_type_ids_are_sent_when_the_model_declares_them() -> None:
    embedder, session = make_embedder()

    embedder.embed_queries(["a"])

    assert session.feeds[0]["token_type_ids"].tolist() == [[0, 0]]
    assert session.feeds[0]["input_ids"].dtype == np.int64


def test_a_tokenizer_without_a_pad_token_is_refused() -> None:
    with pytest.raises(ModelFileError, match="no_pad_token"):
        E5Embedder(FakeSession(), make_tokenizer(with_pad=False))


def test_load_refuses_missing_files_before_opening_a_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[object] = []
    monkeypatch.setattr(
        "deliverius.retrieval.embedder.ort.InferenceSession",
        lambda *args, **kwargs: opened.append(args),
    )

    with pytest.raises(ModelFileError, match=r"onnx/model\.onnx"):
        E5Embedder.load(tmp_path, threads=1)

    assert opened == []
