import asyncio
from typing import Annotated, Literal, Protocol

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel

from deliverius.db.engine import DatabaseHealthProbe
from deliverius.db.errors import DatabaseUnavailableError


class HealthProbe(Protocol):
    async def check_database(self) -> None: ...

    async def has_pgvector(self) -> bool: ...


class HealthChecks(BaseModel):
    database: Literal["ok", "error"]
    pgvector: Literal["ok", "missing", "skipped"]

    @property
    def is_healthy(self) -> bool:
        return self.database == "ok" and self.pgvector == "ok"


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    checks: HealthChecks


def get_health_probe(request: Request) -> HealthProbe:
    return DatabaseHealthProbe(request.app.state.engine)


def get_health_timeout_seconds(request: Request) -> float:
    return float(request.app.state.settings.health_check_timeout_seconds)


async def _inspect(probe: HealthProbe) -> HealthChecks:
    await probe.check_database()
    if not await probe.has_pgvector():
        return HealthChecks(database="ok", pgvector="missing")
    return HealthChecks(database="ok", pgvector="ok")


async def run_checks(probe: HealthProbe, timeout_seconds: float) -> HealthChecks:
    try:
        async with asyncio.timeout(timeout_seconds):
            return await _inspect(probe)
    except (TimeoutError, DatabaseUnavailableError):
        return HealthChecks(database="error", pgvector="skipped")


router = APIRouter(tags=["health"])


@router.get(
    "/health",
    response_model=HealthResponse,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": HealthResponse}},
)
async def health(
    response: Response,
    probe: Annotated[HealthProbe, Depends(get_health_probe)],
    timeout_seconds: Annotated[float, Depends(get_health_timeout_seconds)],
) -> HealthResponse:
    checks = await run_checks(probe, timeout_seconds)
    if checks.is_healthy:
        return HealthResponse(status="ok", checks=checks)
    response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthResponse(status="degraded", checks=checks)
