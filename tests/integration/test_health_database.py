import pytest
from fastapi.testclient import TestClient

from deliverius.api.main import create_app
from deliverius.config import Settings

pytestmark = pytest.mark.integration


def _health_with(
    database_url: str, *, timeout_seconds: float = 5.0
) -> tuple[int, dict[str, object]]:
    settings = Settings(database_url=database_url, database_connect_timeout_seconds=timeout_seconds)
    with TestClient(create_app(settings)) as client:
        response = client.get("/health")
    return response.status_code, response.json()


def test_health_ok_against_migrated_database(migrated_database_url: str) -> None:
    status_code, body = _health_with(migrated_database_url)

    assert status_code == 200
    assert body == {"status": "ok", "checks": {"database": "ok", "pgvector": "ok"}}


def test_health_reports_missing_pgvector_when_extension_is_not_installed(
    database_without_pgvector_url: str,
) -> None:
    status_code, body = _health_with(database_without_pgvector_url)

    assert status_code == 503
    assert body == {"status": "degraded", "checks": {"database": "ok", "pgvector": "missing"}}


def test_health_reports_database_error_when_unreachable() -> None:
    unreachable_url = "postgresql+asyncpg://nobody:nothing@127.0.0.1:1/none"

    status_code, body = _health_with(unreachable_url, timeout_seconds=1.0)

    assert status_code == 503
    assert body == {"status": "degraded", "checks": {"database": "error", "pgvector": "skipped"}}
