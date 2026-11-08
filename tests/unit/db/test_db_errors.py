import asyncio

import pytest
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from structlog.testing import capture_logs

from deliverius.db.errors import (
    DatabaseTimeoutError,
    DatabaseUnavailableError,
    translate_db_errors,
)
from deliverius.shared.errors import DeliveriusError


class DriverError(Exception):
    def __init__(self, sqlstate: str | None) -> None:
        super().__init__("driver failure")
        self.sqlstate = sqlstate


def driver_error(sqlstate: str | None) -> DBAPIError:
    return DBAPIError("SELECT 1", None, DriverError(sqlstate))


def test_database_errors_share_the_application_base() -> None:
    assert issubclass(DatabaseUnavailableError, DeliveriusError)
    assert issubclass(DatabaseTimeoutError, DatabaseUnavailableError)


@pytest.mark.parametrize("sqlstate", ["57014", "55P03"])
def test_statement_and_lock_timeouts_become_timeout_errors(sqlstate: str) -> None:
    with capture_logs() as logs, pytest.raises(DatabaseTimeoutError), translate_db_errors():
        raise driver_error(sqlstate)

    assert logs == [{"event": "db.query_timeout", "log_level": "warning", "sqlstate": sqlstate}]


def test_pool_wait_timeout_becomes_timeout_error() -> None:
    with capture_logs() as logs, pytest.raises(DatabaseTimeoutError), translate_db_errors():
        raise PoolTimeoutError

    assert logs == [{"event": "db.pool_timeout", "log_level": "warning"}]


def test_other_driver_errors_become_unavailable_errors() -> None:
    with (
        capture_logs() as logs,
        pytest.raises(DatabaseUnavailableError) as raised,
        translate_db_errors(),
    ):
        raise driver_error("08006")

    assert not isinstance(raised.value, DatabaseTimeoutError)
    assert logs[0]["event"] == "db.unavailable"
    assert logs[0]["log_level"] == "error"


def test_network_errors_become_unavailable_errors() -> None:
    with capture_logs() as logs, pytest.raises(DatabaseUnavailableError), translate_db_errors():
        raise ConnectionRefusedError

    assert logs == [
        {"event": "db.unavailable", "log_level": "error", "error_type": "ConnectionRefusedError"}
    ]


def test_cancellation_is_never_swallowed() -> None:
    with pytest.raises(asyncio.CancelledError), translate_db_errors():
        raise asyncio.CancelledError


def test_unrelated_errors_pass_through_untouched() -> None:
    with pytest.raises(KeyError), translate_db_errors():
        raise KeyError("unrelated")
