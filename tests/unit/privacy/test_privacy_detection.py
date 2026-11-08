import time

import pytest

from deliverius.privacy import PersonalDataKind, find_personal_data, normalize_text, redact_text

CPF = PersonalDataKind.CPF
CNPJ = PersonalDataKind.CNPJ
CARD = PersonalDataKind.CARD
PHONE = PersonalDataKind.PHONE
EMAIL = PersonalDataKind.EMAIL
POSTAL_CODE = PersonalDataKind.POSTAL_CODE
ADDRESS = PersonalDataKind.ADDRESS


def found(text: str) -> list[tuple[PersonalDataKind, str]]:
    normalized = normalize_text(text)
    return [
        (match.kind, normalized[match.start : match.end])
        for match in find_personal_data(normalized)
    ]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("529.982.247-25", (CPF, "529.982.247-25"), id="cpf punctuated"),
        pytest.param("52998224725", (CPF, "52998224725"), id="cpf plain"),
        pytest.param("529 982 247 25", (CPF, "529 982 247 25"), id="cpf with spaces"),
        pytest.param("529.982.247-26", (CPF, "529.982.247-26"), id="punctuated with wrong digit"),
        pytest.param("12.ABC.345/01DE-35", (CNPJ, "12.ABC.345/01DE-35"), id="cnpj official"),
        pytest.param("12ABC34501DE35", (CNPJ, "12ABC34501DE35"), id="cnpj plain"),
        pytest.param("12abc34501de35", (CNPJ, "12abc34501de35"), id="cnpj lower case"),
        pytest.param("11.222.333/0001-81", (CNPJ, "11.222.333/0001-81"), id="cnpj numeric"),
        pytest.param("11222333000181", (CNPJ, "11222333000181"), id="cnpj numeric plain"),
        pytest.param("11.222.333/0001-82", (CNPJ, "11.222.333/0001-82"), id="cnpj wrong digit"),
        pytest.param("4111 1111 1111 1111", (CARD, "4111 1111 1111 1111"), id="card groups"),
        pytest.param("4111-1111-1111-1111", (CARD, "4111-1111-1111-1111"), id="card hyphens"),
        pytest.param("4111111111111111", (CARD, "4111111111111111"), id="card plain"),
        pytest.param("3782 822463 10005", (CARD, "3782 822463 10005"), id="card amex"),
        pytest.param("+55 11 99570-1543", (PHONE, "+55 11 99570-1543"), id="phone country code"),
        pytest.param("(11) 99570-1543", (PHONE, "(11) 99570-1543"), id="phone parenthesis"),
        pytest.param("(011) 3456-7890", (PHONE, "(011) 3456-7890"), id="phone leading zero"),
        pytest.param("11995701543", (PHONE, "11995701543"), id="mobile plain"),
        pytest.param("3456-7890", (PHONE, "3456-7890"), id="no area code"),
        pytest.param("0800 123 4567", (PHONE, "0800 123 4567"), id="toll free"),
        pytest.param("0300-123-4567", (PHONE, "0300-123-4567"), id="service number"),
        pytest.param("ana.b+1@exemplo.com.br", (EMAIL, "ana.b+1@exemplo.com.br"), id="email"),
        pytest.param("11987654321@exemplo.com", (EMAIL, "11987654321@exemplo.com"), id="digits"),
        pytest.param("01310-100", (POSTAL_CODE, "01310-100"), id="postal code"),
        pytest.param("01.310-100", (POSTAL_CODE, "01.310-100"), id="postal code with dot"),
        pytest.param("01310100", (POSTAL_CODE, "01310100"), id="eight plain digits"),
        pytest.param(
            "moro na Av. Paulista, n\u00ba 1000", (ADDRESS, "Av. Paulista, no 1000"), id="av"
        ),
        pytest.param("Rua das Flores 25", (ADDRESS, "Rua das Flores 25"), id="rua without comma"),
        pytest.param("rua das flores, 25", (ADDRESS, "rua das flores, 25"), id="lower case"),
        pytest.param("R. Augusta, 500", (ADDRESS, "R. Augusta, 500"), id="abbreviation"),
        pytest.param("Praça da Sé 1", (ADDRESS, "Praça da Sé 1"), id="accented street type"),
        pytest.param("Rua 7 de Setembro, 100", (ADDRESS, "Rua 7 de Setembro, 100"), id="date name"),
        pytest.param("Rua 12", (ADDRESS, "Rua 12"), id="only a number"),
    ],
)
def test_recognizes(text: str, expected: tuple[PersonalDataKind, str]) -> None:
    assert found(text) == [expected]


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("12345678901", id="eleven digits, bad check digit, not a phone"),
        pytest.param("ANTICONSTITU12", id="word shaped like a plain cnpj"),
        pytest.param("order12345678x", id="digits inside a word"),
    ],
)
def test_ignores_lookalikes(text: str) -> None:
    assert found(text) == []


def test_card_without_valid_luhn_is_not_a_card() -> None:
    kinds = {kind for kind, _ in found("4111 1111 1111 1112")}

    assert CARD not in kinds


def test_values_come_in_order_of_appearance() -> None:
    assert found("rua a, 10, cpf 52998224725 e ana@exemplo.com") == [
        (ADDRESS, "rua a, 10"),
        (CPF, "52998224725"),
        (EMAIL, "ana@exemplo.com"),
    ]


def test_neighbor_values_are_recognized_separately() -> None:
    assert found("52998224725 11987654321") == [(CPF, "52998224725"), (PHONE, "11987654321")]


@pytest.mark.parametrize(
    "text",
    [
        "paguei R$ 1.234,56 e chegou às 19h30 de 02/11/2026",
        "4 de novembro de 2026",
        "2026-11-02",
        "12:30:45",
        "custa 45,90 e a taxa 7,50",
        "são 2 pizzas e 3 refrigerantes",
        "nota 10 de 10",
        "versão 1.20.1",
        "desconto de 15%",
        "pedido n\u00ba 4521",
        "port 5433",
        "duration_ms=12345",
        "R$ 12.345,00",
        "0193a2b4-7c1e-7f3a-9d2b-123456789012",
        "2026-11-02T03:46:02.123456Z",
        "2026-11-02T03:46:02+00:00",
        "4bf92f3577b34da6a3ce929d0e0e4736",
        "comprei na via 2 vezes",
        "o vale 50 reais",
        "o conjunto 2",
        "o parque 3",
        "rua sem saída",
        "faltou 1 item, 2 latas",
        "minha tv 42 chegou",
    ],
)
def test_ordinary_text_passes_unchanged(text: str) -> None:
    assert redact_text(text) == normalize_text(text)


def test_uuid_with_a_phone_shaped_group_is_protected() -> None:
    uuid = "12345678-1234-7234-8234-123456789012"

    assert redact_text(f"trace {uuid}") == f"trace {uuid}"


def test_accepted_false_positive_street_name_loses_only_the_number() -> None:
    """Written in the design: it costs little and may be a real address."""
    assert redact_text("rua 12 de outubro é longe") == "[ENDERECO] de outubro é longe"


def test_accepted_false_positive_ten_digits_read_as_a_phone() -> None:
    """Written in the design: strings of 10 or 11 digits look like phones; UUIDs are protected."""
    assert redact_text("1793505923-1aa2") == "[TELEFONE]-1aa2"


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("1" * 1000, id="digits"),
        pytest.param("1." * 500, id="dots"),
        pytest.param("1-" * 500, id="hyphens"),
        pytest.param("1 " * 500, id="spaces"),
        pytest.param("A1" * 500, id="alphanumeric"),
        pytest.param("a@" * 500, id="at signs"),
        pytest.param("a." * 500, id="email without at sign"),
        pytest.param("a." * 499 + "@b", id="long email"),
        pytest.param("a@b" + ".c" * 498, id="long domain"),
        pytest.param("rua " * 250, id="street type"),
        pytest.param("rua " + "a " * 498, id="street without number"),
        pytest.param("rua a" * 200, id="glued street"),
        pytest.param("\ufdfa" * 1000, id="grows under NFKC"),
        pytest.param("\u200b" * 1000, id="invisible"),
    ],
)
def test_hostile_input_stays_under_50_ms(text: str) -> None:
    def seconds_to_redact() -> float:
        started = time.perf_counter()
        redact_text(text)
        return time.perf_counter() - started

    # The best of three, so a noisy CI machine does not fail a pattern that is actually linear.
    assert min(seconds_to_redact() for _ in range(3)) < 0.05
