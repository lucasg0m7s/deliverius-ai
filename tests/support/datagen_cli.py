"""The command line on a scripted LLM: wiring, answers by prompt and the files that come out."""

import re
from collections.abc import AsyncIterator, Iterable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from pydantic import SecretStr
from support.datagen_configs import CLI_CONFIG
from support.datagen_llm import Answer, DatagenHarness, Responder, harness, structured
from support.datagen_llm import valid_variations as scenario_variations
from support.llm_runtime import settings_with

from deliverius.cli.wiring import RuntimeFactory, Wiring
from deliverius.config import Settings
from deliverius.datagen.config import DatagenConfig
from deliverius.datagen.messages import render_scenario
from deliverius.datagen.pipeline import CommitInfo
from deliverius.datagen.publish import DatasetHub, HubFactory
from deliverius.datagen.rag_pairs import PolicySection, policy_sections
from deliverius.datagen.scenarios import generate_holdout_scenarios, generate_message_scenarios
from deliverius.llm.in_memory import InMemoryLlmUsageStore
from deliverius.llm.roles import ModelRole
from deliverius.llm.runtime import LlmRuntime

COMMIT = CommitInfo(commit="c" * 40, dirty=True)
_SECTION_BLOCK = re.compile(
    r"documento: (?P<doc>POL-\d\d) · .*\ntitulo_da_secao: (?P<title>.*)\ntexto:\n"
    r"(?P<text>.*)\n</secao>\nquantidade_de_perguntas: (?P<count>\d+)",
    re.DOTALL,
)


def answers_for(config: DatagenConfig) -> tuple[Responder, Responder]:
    """What Gemini (main messages and policy questions) and Groq (the other generator) answer."""
    main = generate_message_scenarios(config)
    holdout = generate_holdout_scenarios(config, main)
    scenarios = {render_scenario(s): s for s in (*main, *holdout)}
    sections = {(s.document, s.title): s for s in policy_sections()}

    def gemini(human: str) -> Answer:
        if human in scenarios:
            return structured({"variations": scenario_variations(scenarios[human])})
        match = _SECTION_BLOCK.search(human)
        assert match is not None, "an unexpected prompt reached the scripted model"
        section = sections[(match["doc"], match["title"])]
        return structured({"questions": questions_of(section, int(match["count"]))})

    def groq(human: str) -> Answer:
        return structured({"variations": scenario_variations(scenarios[human])})

    return gemini, groq


def questions_of(section: PolicySection, count: int) -> list[str]:
    return [
        f"Pergunta {n} do cliente sobre {section.document} e {section.title}?" for n in range(count)
    ]


@dataclass
class FakeHub:
    """A Hub that remembers what it was asked and can be told to fail."""

    existing_tags: frozenset[str] = frozenset()
    fail_with: Exception | None = None
    created: list[str] = field(default_factory=list)
    uploads: list[tuple[str, dict[str, bytes]]] = field(default_factory=list)
    tagged: list[tuple[str, str]] = field(default_factory=list)
    tokens: list[str] = field(default_factory=list)

    def factory(self, token: SecretStr) -> DatasetHub:
        self.tokens.append(token.get_secret_value())
        return self

    def _maybe_fail(self) -> None:
        if self.fail_with is not None:
            raise self.fail_with

    def create_dataset_repo(self, repo_id: str) -> None:
        self._maybe_fail()
        self.created.append(repo_id)

    def tags(self, repo_id: str) -> frozenset[str]:
        self._maybe_fail()
        return self.existing_tags

    def upload_folder(self, repo_id: str, folder: Path, message: str) -> None:
        self._maybe_fail()
        files = {
            path.relative_to(folder).as_posix(): path.read_bytes()
            for path in sorted(folder.rglob("*"))
            if path.is_file()
        }
        self.uploads.append((repo_id, files))

    def create_tag(self, repo_id: str, tag: str) -> None:
        self._maybe_fail()
        self.tagged.append((repo_id, tag))


def runtime_of(h: DatagenHarness) -> RuntimeFactory:
    @asynccontextmanager
    async def factory(settings: Settings, roles: Iterable[ModelRole]) -> AsyncIterator[LlmRuntime]:
        yield h.runtime

    return factory


def never_a_runtime(
    settings: Settings, roles: Iterable[ModelRole]
) -> AbstractAsyncContextManager[LlmRuntime]:
    pytest.fail("this command must not build the LLM runtime")


def wiring_for(
    h: DatagenHarness | None = None,
    *,
    config: DatagenConfig = CLI_CONFIG,
    settings: Settings | None = None,
    hub: HubFactory | None = None,
    commit: CommitInfo = COMMIT,
) -> Wiring:
    return Wiring(
        settings=lambda: settings or settings_with(),
        runtime=runtime_of(h) if h is not None else never_a_runtime,
        commit=lambda: commit,
        hub=hub or FakeHub().factory,
        base_config=config,
    )


def scripted_harness(
    cache: Path,
    config: DatagenConfig = CLI_CONFIG,
    settings: Settings | None = None,
    store: InMemoryLlmUsageStore | None = None,
) -> DatagenHarness:
    gemini, groq = answers_for(config)
    return harness(cache, gemini, groq, settings=settings, store=store)
