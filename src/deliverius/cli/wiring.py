"""What the commands depend on, in one place so a test can swap each piece."""

from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from deliverius.cli.commit import read_commit_info
from deliverius.config import Settings
from deliverius.datagen.config import DatagenConfig
from deliverius.datagen.pipeline import CommitInfo
from deliverius.datagen.publish import HubFactory, huggingface_hub_for
from deliverius.db.engine import create_engine
from deliverius.db.session import create_session_factory
from deliverius.llm.roles import ModelRole
from deliverius.llm.runtime import LlmRuntime
from deliverius.retrieval import (
    E5Embedder,
    IndexReport,
    PolicyIndexer,
    PolicySearch,
    PostgresPolicyChunkRepository,
    ReliabilityRule,
    Searcher,
    fetch_model,
)
from deliverius.shared.clock import SystemClock

type RuntimeFactory = Callable[
    [Settings, Iterable[ModelRole]], AbstractAsyncContextManager[LlmRuntime]
]


@asynccontextmanager
async def database_runtime(
    settings: Settings, roles: Iterable[ModelRole]
) -> AsyncIterator[LlmRuntime]:
    """The production runtime, whose budget lives in Postgres. The engine closes with the block.

    Raises `LlmConfigurationError` if a role in use lacks the key of its provider.
    """
    engine = create_engine(settings)
    try:
        yield LlmRuntime.build(settings, create_session_factory(engine), SystemClock(), roles=roles)
    finally:
        await engine.dispose()


class Reindexer(Protocol):
    async def reindex(self) -> IndexReport: ...


@dataclass(frozen=True, slots=True)
class RetrievalRuntime:
    """What `retrieval index` and `retrieval evaluate` use, behind interfaces a test can fake."""

    indexer: Reindexer
    searcher: Searcher
    count_indexed_documents: Callable[[], Awaitable[int]]


type RetrievalFactory = Callable[[Settings], AbstractAsyncContextManager[RetrievalRuntime]]
type ModelFetcher = Callable[[Path], tuple[str, ...]]


@asynccontextmanager
async def database_retrieval(settings: Settings) -> AsyncIterator[RetrievalRuntime]:
    """The production search: the real model and Postgres. The engine closes with the block.

    Raises `ModelFileError` if a model file is missing or differs from the pinned one.
    """
    embedder = E5Embedder.load(settings.retrieval_model_dir, settings.retrieval_threads)
    engine = create_engine(settings)
    try:
        repository = PostgresPolicyChunkRepository(
            create_session_factory(engine), settings.database_lock_timeout_ms
        )

        async def count_indexed_documents() -> int:
            return len(await repository.read_hashes())

        yield RetrievalRuntime(
            indexer=PolicyIndexer(repository, embedder),
            searcher=PolicySearch(
                embedder,
                repository,
                settings.retrieval_min_similarity,
                ReliabilityRule(settings.retrieval_reliability_rule),
            ),
            count_indexed_documents=count_indexed_documents,
        )
    finally:
        await engine.dispose()


@dataclass(frozen=True, slots=True)
class Wiring:
    settings: Callable[[], Settings] = Settings
    runtime: RuntimeFactory = database_runtime
    commit: Callable[[], CommitInfo] = read_commit_info
    hub: HubFactory = huggingface_hub_for
    retrieval: RetrievalFactory = database_retrieval
    fetch_model: ModelFetcher = fetch_model
    # The sizes and rates; the command line changes only the seed and the concurrency.
    base_config: DatagenConfig = field(default_factory=DatagenConfig)
