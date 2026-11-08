"""What the commands depend on, in one place so a test can swap each piece."""

from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field

from deliverius.cli.commit import read_commit_info
from deliverius.config import Settings
from deliverius.datagen.config import DatagenConfig
from deliverius.datagen.pipeline import CommitInfo
from deliverius.datagen.publish import HubFactory, huggingface_hub_for
from deliverius.db.engine import create_engine
from deliverius.db.session import create_session_factory
from deliverius.llm.roles import ModelRole
from deliverius.llm.runtime import LlmRuntime
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


@dataclass(frozen=True, slots=True)
class Wiring:
    settings: Callable[[], Settings] = Settings
    runtime: RuntimeFactory = database_runtime
    commit: Callable[[], CommitInfo] = read_commit_info
    hub: HubFactory = huggingface_hub_for
    # The sizes and rates; the command line changes only the seed and the concurrency.
    base_config: DatagenConfig = field(default_factory=DatagenConfig)
