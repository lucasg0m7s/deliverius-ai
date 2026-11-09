class DeliveriusError(Exception):
    """Base of every error the application raises on purpose."""


class NotFoundError(DeliveriusError):
    """The object does not exist or is outside the caller's access scope (the two look the same)."""


class ConflictError(DeliveriusError):
    """The operation clashes with the current state (already decided, already executed).

    `code` is stable, such as `approval_already_decided`: the API returns it, tests match on it.
    """

    def __init__(self, code: str = "conflict") -> None:
        super().__init__(code)
        self.code = code


class InvalidInputError(DeliveriusError):
    """Input from outside failed validation (e.g. a malformed or tampered cursor)."""


class ExternalServiceError(DeliveriusError):
    """A dependency outside the process (LLM provider, model hub) failed or timed out."""


class InvariantError(DeliveriusError, ValueError):
    """A domain object was built in a state the domain forbids: always a bug in the caller.

    `rule` is a stable code such as `money.negative`, so tests and logs match on it, not on prose.
    """

    def __init__(self, rule: str) -> None:
        super().__init__(rule)
        self.rule = rule
