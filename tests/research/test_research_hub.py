"""Shared publication: a dry run by default, never over an existing tag, resumable after a crash."""

from pathlib import Path

import pytest
from pydantic import SecretStr
from support.fake_hub import FakeHub

from research.hub import ModelHub, PublishError, Staged, Upload, publish, send


def staged_in(folder: Path) -> Staged:
    (folder / "model.json").write_text("{}", encoding="utf-8")
    return Staged(Upload("x/model", "v1.0.0", folder, "Model v1.0.0"), ("Destino: x/model",))


def upload_in(tmp_path: Path) -> Upload:
    return staged_in(tmp_path).upload


def test_the_dry_run_shows_the_plan_and_sends_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    hub = FakeHub()

    code = publish(staged_in, confirm=False, hub_for=lambda _: hub, prefix="t-")

    out = capsys.readouterr().out
    assert code == 0
    assert hub.repos == hub.uploads == hub.tagged == []
    assert "Destino: x/model" in out
    assert "nada foi enviado" in out


def test_confirm_uploads_and_tags_with_the_token(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HF_TOKEN", "test-token")
    hub = FakeHub()
    tokens: list[SecretStr] = []

    def hub_for(token: SecretStr) -> ModelHub:
        tokens.append(token)
        return hub

    code = publish(staged_in, confirm=True, hub_for=hub_for, prefix="t-")

    assert code == 0
    assert hub.uploads == [{"model.json"}]
    assert hub.tagged == [("v1.0.0", "b" * 40)]
    assert tokens[0].get_secret_value() == "test-token"
    assert "b" * 40 in capsys.readouterr().out


def test_an_existing_tag_is_refused_before_any_upload(tmp_path: Path) -> None:
    hub = FakeHub(existing_tags={"v1.0.0"})

    with pytest.raises(PublishError) as refused:
        send(upload_in(tmp_path), hub)

    assert refused.value.rule == "publish.tag_exists"
    assert hub.uploads == []


def test_a_crash_between_upload_and_tag_is_resumed_by_running_again(tmp_path: Path) -> None:
    hub = FakeHub(fail_on_tag=True)
    upload = upload_in(tmp_path)

    with pytest.raises(ConnectionError):
        send(upload, hub)
    revision = send(upload, hub)

    assert len(hub.uploads) == 2
    assert hub.tagged == [("v1.0.0", revision)]


def test_a_hub_failure_reports_only_the_error_type(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HF_TOKEN", "test-token")

    code = publish(
        staged_in, confirm=True, hub_for=lambda _: FakeHub(fail_on_tag=True), prefix="t-"
    )

    err = capsys.readouterr().err
    assert code == 1
    assert "publish.hub_failed: ConnectionError" in err
    assert "dropped" not in err


def test_without_a_token_nothing_is_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HF_TOKEN", "")
    monkeypatch.chdir(tmp_path)  # no .env here
    hub = FakeHub()

    code = publish(staged_in, confirm=True, hub_for=lambda _: hub, prefix="t-")

    assert code == 1
    assert "publish.token_missing" in capsys.readouterr().err
    assert hub.uploads == []


def test_a_refusal_while_staging_sends_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    hub = FakeHub()

    def refuses(folder: Path) -> Staged:
        raise PublishError("publish.dirty_code", "abc")

    code = publish(refuses, confirm=True, hub_for=lambda _: hub, prefix="t-")

    assert code == 1
    assert "publish.dirty_code" in capsys.readouterr().err
    assert hub.repos == []
