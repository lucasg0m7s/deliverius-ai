from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """A unit of work is `async with factory() as session, session.begin():`, opened by the caller.

    `expire_on_commit=False` keeps loaded objects readable after commit without a lazy reload,
    which async SQLAlchemy cannot do implicitly.
    """
    return async_sessionmaker(engine, expire_on_commit=False)
