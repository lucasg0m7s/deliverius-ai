"""Retrieval: finds the policy section that answers a question, by word and by meaning.

Local embeddings through ONNX Runtime: no PyTorch, no network at run time.
"""

from deliverius.retrieval.embedder import EMBEDDING_DIMENSIONS, E5Embedder
from deliverius.retrieval.model_files import (
    MODEL_FILES,
    MODEL_REVISION,
    ModelFile,
    ModelFileError,
    fetch_model,
    verify_model_files,
)

__all__ = [
    "EMBEDDING_DIMENSIONS",
    "MODEL_FILES",
    "MODEL_REVISION",
    "E5Embedder",
    "ModelFile",
    "ModelFileError",
    "fetch_model",
    "verify_model_files",
]
