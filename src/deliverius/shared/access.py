from dataclasses import dataclass
from typing import assert_never

from deliverius.shared.ids import CustomerId, SandboxId


@dataclass(frozen=True, slots=True)
class CustomerScope:
    """A customer sees only their own data, inside their sandbox."""

    sandbox_id: SandboxId
    customer_id: CustomerId


@dataclass(frozen=True, slots=True)
class SandboxScope:
    """A reviewer or an internal task sees every customer of one sandbox, never another sandbox."""

    sandbox_id: SandboxId


# There is deliberately no "everything" scope: cross-sandbox work gets its own explicit method.
type AccessScope = CustomerScope | SandboxScope


def is_visible(scope: AccessScope, sandbox_id: SandboxId, customer_id: CustomerId) -> bool:
    """Whether data of this customer is inside the scope: the in-memory twin of a `WHERE`."""
    match scope:
        case CustomerScope(sandbox_id=scope_sandbox, customer_id=scope_customer):
            return sandbox_id == scope_sandbox and customer_id == scope_customer
        case SandboxScope(sandbox_id=scope_sandbox):
            return sandbox_id == scope_sandbox
        case _:
            assert_never(scope)
