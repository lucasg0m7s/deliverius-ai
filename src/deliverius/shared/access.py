from dataclasses import dataclass

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
