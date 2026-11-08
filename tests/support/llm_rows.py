from datetime import UTC, datetime
from typing import Any

from deliverius.llm.ports import CallStatus, LlmCallRecord
from deliverius.llm.pricing import TokenUsage
from deliverius.llm.roles import ModelRole
from deliverius.shared.ids import new_id
from deliverius.shared.model_ref import LlmProvider, ModelRef

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
TODAY = NOW.date()
GEMINI_REF = ModelRef(LlmProvider.GEMINI, "gemini-3.5-flash-lite")
SAMPLE_USAGE = TokenUsage(input=1_000, output=200, reasoning=50)


def call_values(**overrides: Any) -> dict[str, Any]:
    """Column values of a valid `llm_calls` row, for inserts that bypass the repository."""
    values: dict[str, Any] = {
        "id": new_id(),
        "created_at": NOW,
        "role": "agent",
        "provider": "gemini",
        "model": "gemini-3.5-flash-lite",
        "status": "ok",
        "latency_ms": 420,
        "input_tokens": 100,
        "output_tokens": 20,
        "reasoning_tokens": 5,
        "real_cost_usd_micros": 0,
        "equivalent_cost_usd_micros": 80,
    }
    return values | overrides


def build_call_record(**overrides: Any) -> LlmCallRecord:
    values: dict[str, Any] = {
        "id": new_id(),
        "created_at": NOW,
        "role": ModelRole.AGENT,
        "provider": LlmProvider.GEMINI,
        "model": "gemini-3.5-flash-lite",
        "status": CallStatus.OK,
        "latency_ms": 420,
        "usage": TokenUsage(input=100, output=20, reasoning=5),
        "real_cost_usd_micros": 0,
        "equivalent_cost_usd_micros": 80,
    }
    return LlmCallRecord(**(values | overrides))
