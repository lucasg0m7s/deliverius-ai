import asyncio
from collections.abc import Iterator
from contextlib import contextmanager

import structlog
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from deliverius.shared.errors import DeliveriusError

logger = structlog.get_logger(__name__)

# SQLSTATE codes: 57014 query_canceled (statement_timeout), 55P03 lock_not_available (lock_timeout).
_TIMEOUT_SQLSTATES = frozenset({"57014", "55P03"})


class DatabaseUnavailableError(DeliveriusError):
    """The database could not be reached or failed while answering."""


class DatabaseTimeoutError(DatabaseUnavailableError):
    """A database limit ran out: statement, lock or the wait for a pooled connection."""


@contextmanager
def translate_db_errors() -> Iterator[None]:
    """Turn driver and pool failures into domain errors.

    Nothing above `db/` sees SQLAlchemy or asyncpg exceptions.

    Never retries: only the caller knows whether the operation is safe to repeat.
    """
    try:
        yield
    except asyncio.CancelledError:
        raise
    except PoolTimeoutError as error:
        logger.warning("db.pool_timeout")
        raise DatabaseTimeoutError from error
    except DBAPIError as error:
        sqlstate = getattr(error.orig, "sqlstate", None)
        if sqlstate in _TIMEOUT_SQLSTATES:
            logger.warning("db.query_timeout", sqlstate=sqlstate)
            raise DatabaseTimeoutError from error
        # Not logger.exception: the driver error text carries SQL parameters (maybe personal data).
        logger.error(  # noqa: TRY400
            "db.unavailable", error_type=type(error.orig).__name__, sqlstate=sqlstate
        )
        raise DatabaseUnavailableError from error
    except OSError as error:
        logger.error("db.unavailable", error_type=type(error).__name__)  # noqa: TRY400
        raise DatabaseUnavailableError from error
