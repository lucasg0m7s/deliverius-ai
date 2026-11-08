from pathlib import Path

import numpy as np
import pytest

from deliverius.config import Settings
from deliverius.retrieval import EMBEDDING_DIMENSIONS, E5Embedder, ModelFileError

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def embedder() -> E5Embedder:
    model_dir = Settings(_env_file=None).retrieval_model_dir  # type: ignore[call-arg]
    try:
        return E5Embedder.load(Path(model_dir), threads=2)
    except ModelFileError as error:
        pytest.fail(
            f"the embedding model is not ready ({error}); "
            "run `python -m deliverius.cli retrieval fetch-model` first",
            pytrace=False,
        )


def test_vectors_have_384_dimensions_and_norm_one(embedder: E5Embedder) -> None:
    vectors = embedder.embed_passages(["O prazo para reclamar é de 24 horas.", "Cupom expirado."])

    assert vectors.shape == (2, EMBEDDING_DIMENSIONS)
    assert vectors.dtype == np.float32
    np.testing.assert_allclose(np.linalg.norm(vectors, axis=1), 1.0, atol=1e-5)


def test_a_paraphrase_is_closer_than_another_subject(embedder: E5Embedder) -> None:
    query = embedder.embed_queries(["meu lanche chegou frio"])[0]
    paraphrase = embedder.embed_queries(["o pedido veio gelado"])[0]
    other_subject = embedder.embed_queries(["como troco minha senha"])[0]

    assert float(query @ paraphrase) > float(query @ other_subject)


def test_a_batch_gives_the_same_vectors_as_one_by_one(embedder: E5Embedder) -> None:
    texts = ["pedido atrasado", "quero cancelar o pedido e receber o estorno em até cinco dias"]

    together = embedder.embed_queries(texts)
    alone = np.vstack([embedder.embed_queries([text]) for text in texts])

    np.testing.assert_allclose(together, alone, atol=1e-4)


def test_text_beyond_512_tokens_is_embedded_without_error(embedder: E5Embedder) -> None:
    vectors = embedder.embed_passages(["pedido atrasado " * 600])

    assert vectors.shape == (1, EMBEDDING_DIMENSIONS)
