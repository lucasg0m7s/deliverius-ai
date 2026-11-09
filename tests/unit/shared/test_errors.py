import pytest

from deliverius.shared import ConflictError, DeliveriusError


def test_conflict_error_has_a_generic_code_by_default() -> None:
    error = ConflictError()

    assert error.code == "conflict"
    assert str(error) == "conflict"


def test_conflict_error_carries_the_stable_code_it_was_given() -> None:
    error = ConflictError("approval_already_decided")

    assert error.code == "approval_already_decided"
    assert str(error) == "approval_already_decided"


def test_conflict_error_is_a_domain_error() -> None:
    with pytest.raises(DeliveriusError):
        raise ConflictError("action_lock_timeout")
