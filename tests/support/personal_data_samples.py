"""Fixed measuring set for the masking recall: Faker pt-BR values inside short sentences.

Everything here is synthetic. These sentences are easy on purpose, so the numbers are an upper
bound; what holds for realistic messages comes from the privacy eval.
"""

import random
import re
from collections.abc import Callable
from typing import Final

from faker import Faker

from deliverius.privacy import find_personal_data, normalize_text

SAMPLES_PER_KIND: Final = 1000
SEED: Final = 20261102
SENTENCES: Final = (
    "meu dado é {} tá?",
    "{}",
    "pode conferir {} por favor",
    "oi, {}, obrigado",
    "Meu cadastro: {}.",
    "anota aí ({}) valeu",
)
COMMON_STREET_TYPES: Final = (
    "Rua",
    "Avenida",
    "Av.",
    "Travessa",
    "Alameda",
    "Estrada",
    "Rodovia",
    "Praça",
    "Largo",
    "Vila",
)
NUMBER_STYLES: Final = (", ", " ", " nº ")
_HAS_DIGIT: Final = re.compile(r"[0-9]")

type ValueSource = Callable[[Faker, random.Random], str]


def _common_address(fake: Faker, rng: random.Random) -> str:
    street = f"{rng.choice(COMMON_STREET_TYPES)} {fake.last_name()}"
    return f"{street}{rng.choice(NUMBER_STYLES)}{rng.randint(1, 9999)}"


def _numbered_faker_address(fake: Faker, _rng: random.Random) -> str:
    while True:
        address = fake.street_address()
        if _HAS_DIGIT.search(address):
            return address


def _plain_cpf(fake: Faker, _rng: random.Random) -> str:
    return re.sub(r"[^0-9]", "", fake.cpf())


# Kinds with a floor: a test fails if any of them drops below it.
STRUCTURED_SOURCES: Final[dict[str, ValueSource]] = {
    "cpf": lambda fake, _: fake.cpf(),
    "cpf_plain": _plain_cpf,
    "cnpj": lambda fake, _: fake.cnpj(),
    "card": lambda fake, _: fake.credit_card_number(),
    "phone": lambda fake, _: fake.phone_number(),
    "mobile": lambda fake, _: fake.cellphone_number(),
    "email": lambda fake, _: fake.email(),
    "postal_code": lambda fake, _: fake.postcode(),
    "common_address": _common_address,
}
# Measured and published as known limits, with no floor.
LIMIT_SOURCES: Final[dict[str, ValueSource]] = {
    "faker_numbered_address": _numbered_faker_address,
    "faker_any_address": lambda fake, _: fake.street_address(),
    "person_name": lambda fake, _: fake.name(),
}


def is_fully_hidden(sentence: str, value: str) -> bool:
    """What matters for privacy: the whole value is covered, whatever the label it got."""
    normalized_sentence = normalize_text(sentence)
    normalized_value = normalize_text(value)
    start = normalized_sentence.index(normalized_value)
    end = start + len(normalized_value)
    return any(
        match.start <= start and match.end >= end
        for match in find_personal_data(normalized_sentence)
    )


def measure_recall(source: ValueSource, samples: int = SAMPLES_PER_KIND) -> float:
    fake = Faker("pt_BR")
    fake.seed_instance(SEED)
    rng = random.Random(SEED)  # noqa: S311 - a reproducible test set, not security
    hidden = 0
    for _ in range(samples):
        value = source(fake, rng)
        hidden += is_fully_hidden(rng.choice(SENTENCES).format(value), value)
    return hidden / samples
