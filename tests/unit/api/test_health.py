import asyncio

from httpx import ASGITransport, AsyncClient, Response

from deliverius.api.health import HealthChecks, get_health_probe, run_checks
from deliverius.api.main import create_app
from deliverius.config import Settings
from deliverius.db.errors import DatabaseUnavailableError

DATABASE_ERROR = {"status": "degraded", "checks": {"database": "error", "pgvector": "skipped"}}


class FakeProbe:
    def __init__(
        self,
        *,
        database_down: bool = False,
        pgvector_installed: bool = True,
        database_fails_during_pgvector_check: bool = False,
    ) -> None:
        self._database_down = database_down
        self._pgvector_installed = pgvector_installed
        self._database_fails_during_pgvector_check = database_fails_during_pgvector_check
        self.pgvector_checked = False

    async def check_database(self) -> None:
        if self._database_down:
            raise DatabaseUnavailableError

    async def has_pgvector(self) -> bool:
        self.pgvector_checked = True
        if self._database_fails_during_pgvector_check:
            raise DatabaseUnavailableError
        return self._pgvector_installed


class HangingProbe:
    async def check_database(self) -> None:
        await asyncio.Event().wait()

    async def has_pgvector(self) -> bool:
        return True


async def _get_health(
    probe: FakeProbe | HangingProbe, settings: Settings | None = None
) -> Response:
    app = create_app(settings or Settings())
    app.dependency_overrides[get_health_probe] = lambda: probe
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.get("/health")


async def test_health_ok_when_database_and_pgvector_respond() -> None:
    response = await _get_health(FakeProbe())

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "checks": {"database": "ok", "pgvector": "ok"}}


async def test_health_degraded_and_skips_pgvector_when_database_is_down() -> None:
    probe = FakeProbe(database_down=True)

    response = await _get_health(probe)

    assert response.status_code == 503
    assert response.json() == DATABASE_ERROR
    assert probe.pgvector_checked is False


async def test_health_degraded_when_pgvector_extension_is_missing() -> None:
    response = await _get_health(FakeProbe(pgvector_installed=False))

    assert response.status_code == 503
    assert response.json() == {
        "status": "degraded",
        "checks": {"database": "ok", "pgvector": "missing"},
    }


async def test_health_reports_database_error_when_database_fails_during_pgvector_check() -> None:
    response = await _get_health(FakeProbe(database_fails_during_pgvector_check=True))

    assert response.status_code == 503
    assert response.json() == DATABASE_ERROR


async def test_health_reports_database_error_when_the_check_exceeds_the_timeout() -> None:
    response = await _get_health(HangingProbe(), Settings(health_check_timeout_seconds=0.01))

    assert response.status_code == 503
    assert response.json() == DATABASE_ERROR


async def test_run_checks_stops_waiting_after_the_timeout() -> None:
    checks = await run_checks(HangingProbe(), timeout_seconds=0.01)

    assert checks == HealthChecks(database="error", pgvector="skipped")
