from deliverius.shared import CustomerId, CustomerScope, SandboxId, SandboxScope, is_visible, new_id

SANDBOX = SandboxId(new_id())
CUSTOMER = CustomerId(new_id())


def test_customer_scope_sees_only_their_own_data_in_their_sandbox() -> None:
    scope = CustomerScope(SANDBOX, CUSTOMER)

    assert is_visible(scope, SANDBOX, CUSTOMER)
    assert not is_visible(scope, SANDBOX, CustomerId(new_id()))
    assert not is_visible(scope, SandboxId(new_id()), CUSTOMER)


def test_sandbox_scope_sees_every_customer_of_its_sandbox_and_no_other() -> None:
    scope = SandboxScope(SANDBOX)

    assert is_visible(scope, SANDBOX, CUSTOMER)
    assert is_visible(scope, SANDBOX, CustomerId(new_id()))
    assert not is_visible(scope, SandboxId(new_id()), CUSTOMER)
