import json
import logging
from datetime import UTC, datetime
from typing import Any

import pytest
import structlog
from structlog.testing import capture_logs
from structlog.typing import EventDict

from deliverius.config import Settings
from deliverius.observability.logging import (
    MAX_REDACTION_DEPTH,
    REDACTED,
    configure_logging,
    is_sensitive_key,
    redact_sensitive_keys,
    text_redactor,
)
from deliverius.orders.models import Customer
from deliverius.shared.ids import CustomerId, SandboxId, new_id

CPF = "123.456.789-09"
EMAIL = "ana@example.com"


def json_lines(output: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in output.splitlines() if line.strip()]


def make_customer() -> Customer:
    return Customer(
        id=CustomerId(new_id()),
        sandbox_id=SandboxId(new_id()),
        created_at=datetime(2026, 11, 1, tzinfo=UTC),
        name="Ana Souza",
        cpf=CPF,
        phone="+55 11 91234-5678",
        email=EMAIL,
        address="Rua das Flores, 10",
    )


@pytest.mark.parametrize(
    "key", ["cpf", "email", "token", "password", "Authorization", "customer_email", "x-api-key"]
)
def test_sensitive_keys_are_recognized(key: str) -> None:
    assert is_sensitive_key(key)


@pytest.mark.parametrize("key", ["event", "order_id", "logger", "amount_cents", "status"])
def test_ordinary_keys_are_kept(key: str) -> None:
    assert not is_sensitive_key(key)


def test_redaction_reaches_nested_values() -> None:
    event: EventDict = {
        "event": "orders.read",
        "cpf": CPF,
        "payload": {"customer": {"email": EMAIL, "order_id": "o-1"}, "items": [{"token": "t"}]},
    }

    redacted = redact_sensitive_keys(event)

    assert redacted == {
        "event": "orders.read",
        "cpf": REDACTED,
        "payload": {
            "customer": {"email": REDACTED, "order_id": "o-1"},
            "items": [{"token": REDACTED}],
        },
    }


def test_json_output_hides_personal_data_and_a_logged_customer(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(Settings(_env_file=None, log_format="json"))  # type: ignore[call-arg]

    structlog.get_logger("test").info(
        "orders.lookup",
        cpf=CPF,
        email=EMAIL,
        token="secret-token",  # noqa: S106 - a fake secret the redaction must hide
        customer=make_customer(),
    )

    output = capsys.readouterr().out
    for secret in (CPF, EMAIL, "secret-token", "Ana Souza", "Rua das Flores"):
        assert secret not in output
    [line] = json_lines(output)
    assert line["event"] == "orders.lookup"
    assert line["cpf"] == REDACTED
    assert line["level"] == "info"
    assert line["timestamp"].endswith("Z")


def test_standard_library_logs_share_the_pipeline(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(Settings(_env_file=None, log_format="json"))  # type: ignore[call-arg]

    logging.getLogger("uvicorn.error").warning("server %s", "ready")

    [line] = json_lines(capsys.readouterr().out)
    assert line["event"] == "server ready"
    assert line["level"] == "warning"
    assert line["logger"] == "uvicorn.error"


def test_extra_redactors_run_after_the_key_redaction(capsys: pytest.CaptureFixture[str]) -> None:
    def mask_digits(event: EventDict) -> EventDict:
        return {key: "#" if key == "note" else value for key, value in event.items()}

    configure_logging(
        Settings(_env_file=None, log_format="json"),  # type: ignore[call-arg]
        extra_redactors=[mask_digits],
    )

    structlog.get_logger("test").info("agent.note", note="call 11 91234-5678")

    [line] = json_lines(capsys.readouterr().out)
    assert line["note"] == "#"


def test_console_format_is_readable_and_redacted(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(Settings(_env_file=None, log_format="console"))  # type: ignore[call-arg]

    structlog.get_logger("test").info("orders.lookup", email=EMAIL)

    output = capsys.readouterr().out
    assert "orders.lookup" in output
    assert EMAIL not in output


def test_level_filter_applies(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(Settings(_env_file=None, log_format="json", log_level="WARNING"))  # type: ignore[call-arg]

    structlog.get_logger("test").info("app.noise")

    assert capsys.readouterr().out == ""


def test_capture_logs_still_works_after_configuration() -> None:
    configure_logging(Settings(_env_file=None))  # type: ignore[call-arg]

    with capture_logs() as logs:
        structlog.get_logger("test").info("app.checked")

    assert logs == [{"event": "app.checked", "log_level": "info"}]


class TestExceptionText:
    """Exceptions were formatted after the redaction, so their text reached the log unfiltered."""

    @staticmethod
    def hide_cpf(event: EventDict) -> EventDict:
        return {
            key: value.replace(CPF, "[hidden]") if isinstance(value, str) else value
            for key, value in event.items()
        }

    @pytest.mark.parametrize("log_format", ["json", "console"])
    def test_structlog_exception_is_redacted(
        self, log_format: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        configure_logging(
            Settings(_env_file=None, log_format=log_format),  # type: ignore[call-arg]
            extra_redactors=[self.hide_cpf],
        )

        try:
            raise ValueError(CPF)  # noqa: TRY301 - the log needs a real traceback
        except ValueError:
            structlog.get_logger("test").exception("orders.failed")

        output = capsys.readouterr().out
        assert CPF not in output
        assert "ValueError" in output
        assert "[hidden]" in output

    @pytest.mark.parametrize("log_format", ["json", "console"])
    def test_standard_library_exception_is_redacted(
        self, log_format: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        configure_logging(
            Settings(_env_file=None, log_format=log_format),  # type: ignore[call-arg]
            extra_redactors=[self.hide_cpf],
        )

        try:
            raise ValueError(CPF)  # noqa: TRY301 - the log needs a real traceback
        except ValueError:
            logging.getLogger("library").exception("library failed")

        output = capsys.readouterr().out
        assert CPF not in output
        assert "ValueError" in output
        assert "[hidden]" in output


class TestTextRedactor:
    @staticmethod
    def mask(text: str) -> str:
        return text.replace(CPF, "[CPF]").replace(EMAIL, "[EMAIL]")

    def redact(self, **event: Any) -> EventDict:
        return text_redactor(self.mask)(event)

    def test_message_and_ordinary_fields_are_masked(self) -> None:
        assert self.redact(event=f"cpf {CPF}", note=f"is {CPF}") == {
            "event": "cpf [CPF]",
            "note": "is [CPF]",
        }

    def test_nested_values_and_their_keys_are_masked(self) -> None:
        redacted = self.redact(data={"notes": [f"a {CPF}", (EMAIL,)], CPF: 1, 7: "seven"})

        assert redacted["data"] == {"notes": ["a [CPF]", ["[EMAIL]"]], "[CPF]": 1, 7: "seven"}

    def test_object_representation_is_masked(self) -> None:
        class Contact:
            def __repr__(self) -> str:
                return f"Contact({EMAIL})"

        assert self.redact(contact=Contact())["contact"] == "Contact([EMAIL])"

    def test_object_whose_representation_fails_does_not_fail_the_log(self) -> None:
        class Broken:
            def __repr__(self) -> str:
                raise RuntimeError(CPF)

        assert self.redact(broken=Broken())["broken"] == REDACTED

    @pytest.mark.parametrize(
        "value", [None, True, 12345678, 123.5], ids=["none", "bool", "int", "float"]
    )
    def test_numbers_and_flags_pass_unchanged(self, value: Any) -> None:
        assert self.redact(field=value)["field"] == value

    def test_structure_at_the_depth_limit_is_kept_and_beyond_it_is_redacted(self) -> None:
        deep: Any = CPF
        for _ in range(MAX_REDACTION_DEPTH):
            deep = [deep]
        too_deep: Any = [deep]

        assert self.redact(field=deep)["field"] != REDACTED
        kept = self.redact(field=too_deep)["field"]
        for _ in range(MAX_REDACTION_DEPTH):
            kept = kept[0]
        assert kept == REDACTED


@pytest.mark.parametrize("log_format", ["json", "console"])
def test_circular_structure_does_not_fail_the_log(
    log_format: str, capsys: pytest.CaptureFixture[str]
) -> None:
    circular: list[Any] = [CPF]
    circular.append(circular)
    configure_logging(
        Settings(_env_file=None, log_format=log_format),  # type: ignore[call-arg]
        extra_redactors=[text_redactor(lambda text: text.replace(CPF, "[CPF]"))],
    )

    structlog.get_logger("test").info("orders.read", data=circular)

    output = capsys.readouterr().out
    assert "orders.read" in output
    assert REDACTED in output
    assert CPF not in output


def test_key_redaction_alone_survives_a_circular_mapping() -> None:
    circular: dict[str, Any] = {}
    circular["self"] = circular

    assert REDACTED in str(redact_sensitive_keys({"data": circular}))
