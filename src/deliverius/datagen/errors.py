from deliverius.llm.call import DegradedReason
from deliverius.shared import DeliveriusError


class BudgetExhaustedError(DeliveriusError):
    """The quota ran out (or the provider asked to wait) before every job finished.

    Nothing is written. The answers already obtained are in the cache, so the next run pays only
    for the `remaining` jobs.
    """

    def __init__(self, remaining: int, reason: DegradedReason) -> None:
        super().__init__(f"datagen.budget_exhausted: {remaining} left ({reason})")
        self.remaining = remaining
        self.reason = reason
