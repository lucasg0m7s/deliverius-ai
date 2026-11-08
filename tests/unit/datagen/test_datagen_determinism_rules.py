"""Nothing in `datagen` may read the clock, the global generator or random UUIDs."""

from pathlib import Path

import pytest
from support.nondeterminism_scan import Nondeterminism, find_nondeterminism

DATAGEN_ROOT = Path(__file__).resolve().parents[3] / "src" / "deliverius" / "datagen"


def test_no_datagen_module_uses_a_source_of_nondeterminism() -> None:
    violations: list[Nondeterminism] = []
    for path in sorted(DATAGEN_ROOT.rglob("*.py")):
        relative = str(path.relative_to(DATAGEN_ROOT.parents[2]))
        violations += find_nondeterminism(path.read_text(encoding="utf-8"), relative)

    assert violations == []


def test_the_scan_covers_the_datagen_sources() -> None:
    assert len(list(DATAGEN_ROOT.rglob("*.py"))) >= 8


@pytest.mark.parametrize(
    "source",
    [
        "import random\nrandom.random()\n",
        "import random\nrandom.choice([1, 2])\n",
        "import random\nrandom.seed(1)\n",
        "import random\nrandom.SystemRandom()\n",
        "from random import choice\n",
        "from datetime import datetime\ndatetime.now()\n",
        "import datetime\ndatetime.datetime.utcnow()\n",
        "from datetime import date\ndate.today()\n",
        "import time\ntime.time()\n",
        "import time\ntime.monotonic()\n",
        "from time import time\n",
        "import uuid_utils\nuuid_utils.uuid7()\n",
        "from uuid_utils.compat import uuid7\n",
        "import uuid\nuuid.uuid4()\n",
        "from deliverius.shared import new_id\n",
        "import os\nos.urandom(8)\n",
        "import secrets\n",
        "from faker import Faker\nFaker.seed(1)\n",
    ],
    ids=[
        "random-random",
        "random-choice",
        "random-seed",
        "system-random",
        "from-random",
        "datetime-now",
        "datetime-utcnow",
        "date-today",
        "time-time",
        "time-monotonic",
        "from-time",
        "uuid7-attribute",
        "uuid7-import",
        "uuid4",
        "new-id",
        "urandom",
        "secrets",
        "faker-class-seed",
    ],
)
def test_the_scan_finds_each_source_of_nondeterminism(source: str) -> None:
    assert find_nondeterminism(source)


@pytest.mark.parametrize(
    "source",
    [
        "import random\nrng = random.Random(7)\nrng.random()\n",
        "from random import Random\nRandom(7)\n",
        "import random\ndef f(rng: random.Random) -> None: ...\n",
        "from datetime import datetime, timedelta\ndatetime(2026, 1, 1) + timedelta(days=1)\n",
        "faker.seed_instance(3)\n",
        "ids.next_id(at)\n",
    ],
    ids=[
        "seeded",
        "from-random-random",
        "annotation",
        "fixed-datetime",
        "seed-instance",
        "own-ids",
    ],
)
def test_the_scan_allows_seeded_randomness_and_fixed_instants(source: str) -> None:
    assert find_nondeterminism(source) == []
