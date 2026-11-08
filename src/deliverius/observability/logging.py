"""Structured logs: one pipeline for structlog and the standard library, with redaction built in.

Redaction here is the second barrier. The first is never logging personal data at all. The
free-text masker comes in as a function (`text_redactor`), so this utility never imports `privacy`.
"""

import logging
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import structlog
from structlog.typing import EventDict, Processor, WrappedLogger

from deliverius.config import Settings

type Redactor = Callable[[EventDict], EventDict]

REDACTED = "[REDACTED]"
# Also what stops a circular structure from taking the request down with it.
MAX_REDACTION_DEPTH = 8
SENSITIVE_KEYS = frozenset(
    {
        "password",
        "token",
        "authorization",
        "secret",
        "api_key",
        "cpf",
        "email",
        "phone",
        "name",
        "address",
    }
)
# Libraries that install their own handlers; they are routed through the root pipeline instead.
_LIBRARY_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


def is_sensitive_key(key: str) -> bool:
    """`email`, `customer_email`, `Access-Token` and `DATABASE_PASSWORD` all count."""
    normalized = key.lower().replace("-", "_")
    return any(
        normalized == sensitive or normalized.endswith(f"_{sensitive}")
        for sensitive in SENSITIVE_KEYS
    )


def redact_sensitive_keys(event_dict: EventDict) -> EventDict:
    return {key: _redact(key, value) for key, value in event_dict.items()}


def _redact(key: str, value: Any, depth: int = 0) -> Any:
    if is_sensitive_key(key):
        return REDACTED
    if isinstance(value, Mapping | list | tuple) and depth >= MAX_REDACTION_DEPTH:
        return REDACTED
    if isinstance(value, Mapping):
        return {
            inner_key: _redact(str(inner_key), item, depth + 1) for inner_key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [_redact("", item, depth + 1) for item in value]
    return value


def text_redactor(mask: Callable[[str], str]) -> Redactor:
    """Applies `mask` to every text of an event: message, fields, nested values, object reprs."""

    def redact(event_dict: EventDict) -> EventDict:
        return {key: _mask_value(value, mask, 0) for key, value in event_dict.items()}

    return redact


def _mask_value(value: Any, mask: Callable[[str], str], depth: int) -> Any:
    if value is None or isinstance(value, bool | int | float):
        # Integers stay: `duration_ms` and `total_cents` are legitimate, and 8 digits read as a CEP.
        return value
    if isinstance(value, str):
        return mask(value)
    if isinstance(value, Mapping | list | tuple):
        if depth >= MAX_REDACTION_DEPTH:
            return REDACTED
        if isinstance(value, Mapping):
            return {
                mask(inner_key) if isinstance(inner_key, str) else inner_key: _mask_value(
                    item, mask, depth + 1
                )
                for inner_key, item in value.items()
            }
        return [_mask_value(item, mask, depth + 1) for item in value]
    return _mask_repr(value, mask)


def _mask_repr(value: Any, mask: Callable[[str], str]) -> str:
    try:
        return mask(repr(value))
    except Exception:  # any `__repr__` can raise, and a log must never fail a request
        return REDACTED


def _redaction_processor(redactors: Sequence[Redactor]) -> Processor:
    def redact(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
        for redactor in redactors:
            event_dict = redactor(event_dict)
        return event_dict

    return redact


def configure_logging(settings: Settings, extra_redactors: Sequence[Redactor] = ()) -> None:
    redactors = [redact_sensitive_keys, *extra_redactors]
    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        # Before the redaction, or the text of an exception reaches the log unfiltered.
        structlog.processors.format_exc_info,
        _redaction_processor(redactors),
    ]
    renderer: list[Processor] = (
        [structlog.processors.JSONRenderer()]
        if settings.log_format == "json"
        # Plain traceback: the exception is already text, and the rich box would show locals.
        else [structlog.dev.ConsoleRenderer(exception_formatter=structlog.dev.plain_traceback)]
    )
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        # Off on purpose: cached loggers would ignore structlog.testing.capture_logs in tests.
        cache_logger_on_first_use=False,
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,
            processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, *renderer],
        )
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level)
    for name in _LIBRARY_LOGGERS:
        library_logger = logging.getLogger(name)
        library_logger.handlers = []
        library_logger.propagate = True
