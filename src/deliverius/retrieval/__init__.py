"""Retrieval: finds the policy section that answers a question, by word and by meaning.

Local embeddings through ONNX Runtime: no PyTorch, no network at run time.
"""

from deliverius.retrieval.chunking import (
    ChunkedDocument,
    ChunkingError,
    PolicyChunk,
    chunk_policy_documents,
    section_slug,
)
from deliverius.retrieval.embedder import EMBEDDING_DIMENSIONS, E5Embedder
from deliverius.retrieval.evaluation import (
    EvaluationDataError,
    EvaluationReport,
    QaPair,
    Searcher,
    evaluate,
    evaluate_mode,
    load_out_of_scope,
    load_pairs,
)
from deliverius.retrieval.indexer import IndexReport, PolicyIndexer
from deliverius.retrieval.model_files import (
    MODEL_FILES,
    MODEL_REVISION,
    ModelDownloadError,
    ModelFile,
    ModelFileError,
    fetch_model,
    verify_model_files,
)
from deliverius.retrieval.repository import PostgresPolicyChunkRepository
from deliverius.retrieval.search import (
    MAX_QUERY_CHARS,
    PolicySearch,
    ReliabilityRule,
    SearchHit,
    SearchMode,
    SearchResult,
)

__all__ = [
    "EMBEDDING_DIMENSIONS",
    "MAX_QUERY_CHARS",
    "MODEL_FILES",
    "MODEL_REVISION",
    "ChunkedDocument",
    "ChunkingError",
    "E5Embedder",
    "EvaluationDataError",
    "EvaluationReport",
    "IndexReport",
    "ModelDownloadError",
    "ModelFile",
    "ModelFileError",
    "PolicyChunk",
    "PolicyIndexer",
    "PolicySearch",
    "PostgresPolicyChunkRepository",
    "QaPair",
    "ReliabilityRule",
    "SearchHit",
    "SearchMode",
    "SearchResult",
    "Searcher",
    "chunk_policy_documents",
    "evaluate",
    "evaluate_mode",
    "fetch_model",
    "load_out_of_scope",
    "load_pairs",
    "section_slug",
    "verify_model_files",
]
