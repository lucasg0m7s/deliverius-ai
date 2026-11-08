import pytest
from faker import Faker

from deliverius.privacy.documents import is_valid_cnpj, is_valid_cpf, passes_luhn


@pytest.fixture
def fake() -> Faker:
    faker = Faker("pt_BR")
    faker.seed_instance(4)
    return faker


@pytest.mark.parametrize("cpf", ["52998224725", "11144477735"])
def test_valid_cpf(cpf: str) -> None:
    assert is_valid_cpf(cpf)


@pytest.mark.parametrize(
    "cpf",
    [
        pytest.param("52998224726", id="wrong second digit"),
        pytest.param("52998224735", id="wrong first digit"),
        pytest.param("5299822472", id="too short"),
        pytest.param("529982247250", id="too long"),
        pytest.param("5299822472a", id="not a number"),
    ],
)
def test_invalid_cpf(cpf: str) -> None:
    assert not is_valid_cpf(cpf)


def test_every_cpf_from_the_generator_is_valid(fake: Faker) -> None:
    for _ in range(200):
        assert is_valid_cpf(fake.cpf().replace(".", "").replace("-", ""))


@pytest.mark.parametrize(
    "cnpj",
    [
        pytest.param("12ABC34501DE35", id="official Receita Federal example"),
        pytest.param("12abc34501de35", id="lower case"),
        pytest.param("11222333000181", id="numeric"),
    ],
)
def test_valid_cnpj(cnpj: str) -> None:
    assert is_valid_cnpj(cnpj)


@pytest.mark.parametrize(
    "cnpj",
    [
        pytest.param("12ABC34501DE36", id="wrong second digit"),
        pytest.param("12ABC34501DE45", id="wrong first digit"),
        pytest.param("11222333000182", id="numeric with wrong digit"),
        pytest.param("12ABC34501DE3", id="too short"),
        pytest.param("12ABC34501DE355", id="too long"),
        pytest.param("12ABC-4501DE35", id="symbol inside"),
        pytest.param("12ÁBC34501DE35", id="non-ascii letter"),
    ],
)
def test_invalid_cnpj(cnpj: str) -> None:
    assert not is_valid_cnpj(cnpj)


def test_every_cnpj_from_the_generator_is_valid(fake: Faker) -> None:
    for _ in range(200):
        assert is_valid_cnpj("".join(char for char in fake.cnpj() if char.isdigit()))


def test_check_digit_is_zero_when_the_remainder_is_below_two() -> None:
    """Fixes the `remainder < 2` edge of modulo 11: 000000000191 is a known CNPJ ending in 91."""
    assert is_valid_cnpj("00000000000191")
    assert is_valid_cpf("00000000000")


@pytest.mark.parametrize("card", ["4111111111111111", "5555555555554444", "378282246310005"])
def test_luhn_accepts_valid_cards(card: str) -> None:
    assert passes_luhn(card)


@pytest.mark.parametrize("card", ["4111111111111112", "411111111111111a"])
def test_luhn_rejects_invalid_cards(card: str) -> None:
    assert not passes_luhn(card)
