from datetime import date

import pytest
from support.llm_usage_contract import LlmUsageStoreContract, UsageHarness

from deliverius.llm.in_memory import InMemoryLlmUsageStore
from deliverius.llm.ports import BudgetUsage, LlmCallRecord
from deliverius.shared.model_ref import LlmProvider


class InMemoryHarness:
    def __init__(self) -> None:
        self.store = InMemoryLlmUsageStore()

    async def usage(self, provider: LlmProvider, day: date) -> BudgetUsage | None:
        return self.store.usage(provider, day)

    async def calls(self) -> list[LlmCallRecord]:
        return self.store.calls


class TestInMemoryLlmUsageStore(LlmUsageStoreContract):
    @pytest.fixture
    def harness(self) -> UsageHarness:
        return InMemoryHarness()
