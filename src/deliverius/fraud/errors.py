from deliverius.shared.errors import DeliveriusError


class FraudModelError(DeliveriusError):
    """The model folder cannot be trusted or read; `rule` is a stable code, `file` the culprit."""

    def __init__(self, rule: str, file: str | None = None) -> None:
        super().__init__(rule if file is None else f"{rule}: {file}")
        self.rule = rule
        self.file = file


class FraudScoringError(DeliveriusError):
    """A claim could not be scored. The agent sends the case to a person (fail closed)."""

    def __init__(self, rule: str = "fraud_scoring.failed") -> None:
        super().__init__(rule)
        self.rule = rule
