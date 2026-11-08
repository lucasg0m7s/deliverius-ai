import json

import pytest
import structlog
from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine

from deliverius.api.health import get_health_probe
from deliverius.api.main import create_app
from deliverius.config import Settings
from deliverius.db.engine import DatabaseHealthProbe


def test_lifespan_exposes_engine_on_app_state_while_running() -> None:
    app = create_app(Settings())

    with TestClient(app):
        assert isinstance(app.state.engine, AsyncEngine)


def test_health_probe_dependency_wraps_the_app_engine() -> None:
    app = create_app(Settings())

    with TestClient(app):
        probe = get_health_probe(Request({"type": "http", "app": app}))

    assert isinstance(probe, DatabaseHealthProbe)


def test_startup_logs_app_started_without_secrets(capsys: pytest.CaptureFixture[str]) -> None:
    secret_url = "postgresql+asyncpg://user:do-not-log-me@db.internal:5432/deliverius"  # noqa: S105
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url=secret_url,
        environment="ci",
        log_format="json",
    )

    with TestClient(create_app(settings)):
        pass

    output = capsys.readouterr().out
    started = [json.loads(line) for line in output.splitlines() if '"app.started"' in line]
    assert started == [
        {
            "event": "app.started",
            "environment": "ci",
            "version": "0.1.0",
            "level": "info",
            "logger": "deliverius.api.main",
            "timestamp": started[0]["timestamp"],
        }
    ]
    assert "do-not-log-me" not in output
    assert "db.internal" not in output


def test_logs_of_the_running_app_mask_free_text(capsys: pytest.CaptureFixture[str]) -> None:
    trace_id = "0193a2b4-7c1e-7f3a-9d2b-123456789012"
    settings = Settings(_env_file=None, log_format="json")  # type: ignore[call-arg]

    with TestClient(create_app(settings)):
        structlog.get_logger("test").info(
            "agent.note", note="cpf 529.982.247-25", trace_id=trace_id
        )

    output = capsys.readouterr().out
    line = next(json.loads(row) for row in output.splitlines() if "agent.note" in row)
    assert line["note"] == "cpf [CPF]"
    assert line["trace_id"] == trace_id
    assert "T" in line["timestamp"]
    assert line["timestamp"].endswith("Z")
