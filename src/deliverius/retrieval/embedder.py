"""E5 embeddings on ONNX Runtime and `tokenizers`: no PyTorch, no network.

The recipe is the one of the model card: `query: ` or `passage: ` prefix, tokens truncated at 512,
mean of `last_hidden_state` weighted by the attention mask, then L2 normalisation. The prefixes are
applied here and nowhere else, so a caller cannot forget them.

Vectors leave this module as `float32` numpy arrays of shape `(texts, 384)`; the repository hands
them to pgvector as they are.
"""

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final, Protocol

import numpy as np
import onnxruntime as ort
from numpy.typing import NDArray
from tokenizers import Tokenizer

from deliverius.retrieval.model_files import (
    ONNX_MODEL,
    TOKENIZER,
    ModelFileError,
    verify_model_files,
)

EMBEDDING_DIMENSIONS: Final = 384
MAX_TOKENS: Final = 512
# Bounds the memory of one ONNX run: 512 tokens x 16 texts is a few MB of activations.
BATCH_SIZE: Final = 16
QUERY_PREFIX: Final = "query: "
PASSAGE_PREFIX: Final = "passage: "
_PAD_TOKEN: Final = "<pad>"  # noqa: S105 - the padding token of the tokenizer, not a secret
_NORM_FLOOR: Final = 1e-12

type Embeddings = NDArray[np.float32]


class _NamedInput(Protocol):
    @property
    def name(self) -> str: ...


class InferenceSession(Protocol):
    """The part of `onnxruntime.InferenceSession` the embedder uses."""

    def get_inputs(self) -> Sequence[_NamedInput]: ...

    def run(
        self, output_names: list[str] | None, input_feed: Mapping[str, Any]
    ) -> Sequence[Any]: ...


class E5Embedder:
    def __init__(self, session: InferenceSession, tokenizer: Tokenizer) -> None:
        self._session = session
        self._input_names = tuple(model_input.name for model_input in session.get_inputs())
        self._tokenizer = tokenizer
        self._tokenizer.enable_truncation(max_length=MAX_TOKENS)
        self._tokenizer.enable_padding(pad_id=_pad_id(tokenizer), pad_token=_PAD_TOKEN)

    @classmethod
    def load(cls, model_dir: Path, threads: int) -> "E5Embedder":
        """Verify the model files, then open the session: a wrong file never reaches the runtime."""
        verify_model_files(model_dir, (ONNX_MODEL, TOKENIZER))
        options = ort.SessionOptions()
        options.intra_op_num_threads = threads
        options.inter_op_num_threads = 1
        session = ort.InferenceSession(
            str(model_dir / ONNX_MODEL.name), options, providers=["CPUExecutionProvider"]
        )
        return cls(session, Tokenizer.from_file(str(model_dir / TOKENIZER.name)))

    def embed_queries(self, texts: Sequence[str]) -> Embeddings:
        return self._embed([QUERY_PREFIX + text for text in texts])

    def embed_passages(self, texts: Sequence[str]) -> Embeddings:
        return self._embed([PASSAGE_PREFIX + text for text in texts])

    def _embed(self, prefixed: Sequence[str]) -> Embeddings:
        batches = [
            self._embed_batch(prefixed[start : start + BATCH_SIZE])
            for start in range(0, len(prefixed), BATCH_SIZE)
        ]
        if not batches:
            return np.empty((0, EMBEDDING_DIMENSIONS), dtype=np.float32)
        return np.concatenate(batches)

    def _embed_batch(self, texts: Sequence[str]) -> Embeddings:
        encodings = self._tokenizer.encode_batch(list(texts))
        tensors = {
            "input_ids": np.array([e.ids for e in encodings], dtype=np.int64),
            "attention_mask": np.array([e.attention_mask for e in encodings], dtype=np.int64),
            "token_type_ids": np.array([e.type_ids for e in encodings], dtype=np.int64),
        }
        feed = {name: tensors[name] for name in self._input_names}
        hidden_states: NDArray[np.float32] = self._session.run(None, feed)[0]
        return _normalize(_masked_mean(hidden_states, tensors["attention_mask"]))


def _pad_id(tokenizer: Tokenizer) -> int:
    pad_id = tokenizer.token_to_id(_PAD_TOKEN)
    if pad_id is None:
        raise ModelFileError("model_file.no_pad_token", TOKENIZER.name)
    return pad_id


def _masked_mean(hidden_states: NDArray[np.float32], mask: NDArray[np.int64]) -> Embeddings:
    weights = mask[:, :, np.newaxis].astype(np.float32)
    summed = (hidden_states * weights).sum(axis=1)
    counts = np.maximum(weights.sum(axis=1), 1.0)
    return (summed / counts).astype(np.float32)


def _normalize(vectors: Embeddings) -> Embeddings:
    norms = np.maximum(np.linalg.norm(vectors, axis=1, keepdims=True), _NORM_FLOOR)
    return (vectors / norms).astype(np.float32)
