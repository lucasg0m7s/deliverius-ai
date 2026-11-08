from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from deliverius.api import health
from deliverius.config import Settings, get_settings
from deliverius.db.engine import create_engine
from deliverius.observability import configure_logging, text_redactor
from deliverius.privacy import redact_text

VERSION = "0.1.0"


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(resolved_settings, extra_redactors=[text_redactor(redact_text)])
        # Only non-secret facts: never the settings object, which holds the database URL.
        structlog.get_logger(__name__).info(
            "app.started", environment=resolved_settings.environment, version=VERSION
        )
        engine = create_engine(resolved_settings)
        app.state.engine = engine
        try:
            yield
        finally:
            await engine.dispose()

    app = FastAPI(
        title="Deliverius AI",
        summary="Deli, the AI customer-support agent of Deliverius.",
        version=VERSION,
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings
    app.include_router(health.router)
    return app


app = create_app()
