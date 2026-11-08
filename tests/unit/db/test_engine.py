from deliverius.config import Settings
from deliverius.db.engine import create_engine, server_settings
from deliverius.db.session import create_session_factory


def test_server_settings_come_from_settings() -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_statement_timeout_ms=1234,
        database_lock_timeout_ms=567,
        database_idle_in_transaction_timeout_ms=8910,
    )

    assert server_settings(settings) == {
        "statement_timeout": "1234",
        "lock_timeout": "567",
        "idle_in_transaction_session_timeout": "8910",
    }


async def test_engine_pool_is_bounded_by_settings() -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_pool_size=3,
        database_max_overflow=1,
        database_pool_timeout_seconds=0.5,
    )
    engine = create_engine(settings)
    try:
        pool = engine.pool
        assert pool.size() == 3  # type: ignore[attr-defined]
        assert pool._max_overflow == 1  # type: ignore[attr-defined]
        assert pool._timeout == 0.5  # type: ignore[attr-defined]
    finally:
        await engine.dispose()


async def test_session_factory_keeps_objects_after_commit() -> None:
    engine = create_engine(Settings(_env_file=None))  # type: ignore[call-arg]
    try:
        factory = create_session_factory(engine)
        assert factory.kw["expire_on_commit"] is False
    finally:
        await engine.dispose()
