"""`retrieval fetch-model | index | evaluate`: arguments, Portuguese output and exit codes."""

import io
from collections.abc import AsyncIterator
from contextlib import (
    AbstractAsyncContextManager,
    asynccontextmanager,
    redirect_stderr,
    redirect_stdout,
)
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from support.llm_runtime import settings_with
from support.retrieval_doubles import hit

from deliverius.cli.datagen import main
from deliverius.cli.wiring import RetrievalRuntime, Wiring, database_retrieval
from deliverius.config import Settings
from deliverius.db.errors import DatabaseTimeoutError, DatabaseUnavailableError
from deliverius.retrieval import (
    ChunkingError,
    IndexReport,
    ModelDownloadError,
    ModelFileError,
    SearchMode,
    SearchResult,
)

PAIRS = (
    '{"document": "POL-03", "section": "Prazo", "question": "Quanto tempo tenho?", "id": "1"}\n'
    '{"document": "POL-01", "section": "Prazo", "question": "Até quando cancelo?", "id": "2"}\n'
)
RECALCULATED = ("POL-01", "POL-02")
KEPT = ("POL-03",)
TRUSTED_HIT = hit("POL-03#prazo", section="Prazo", lexical=1, semantic=1)
OUT_OF_SCOPE = '{"id": "oos-1", "question": "Previsão do tempo?"}\n'


@dataclass
class FakeIndexer:
    report: IndexReport = field(default_factory=lambda: IndexReport(RECALCULATED, KEPT, ()))
    error: Exception | None = None
    runs: int = 0

    async def reindex(self) -> IndexReport:
        self.runs += 1
        if self.error is not None:
            raise self.error
        return self.report


@dataclass
class FakeSearcher:
    answers: dict[str, SearchResult] = field(default_factory=dict)

    async def search(self, query: str, mode: SearchMode) -> SearchResult:
        if query in self.answers:
            return self.answers[query]
        return SearchResult(
            (hit("POL-03#prazo", section="Prazo", lexical=1, semantic=1),), True, 0.9, True
        )


@dataclass
class Harness:
    indexer: FakeIndexer = field(default_factory=FakeIndexer)
    searcher: FakeSearcher = field(default_factory=FakeSearcher)
    indexed_documents: int = 12
    model_error: Exception | None = None
    fetched: list[Path] = field(default_factory=list)
    downloaded: tuple[str, ...] = ()
    fetch_error: Exception | None = None
    opened: int = 0

    def retrieval(self, settings: Settings) -> AbstractAsyncContextManager[RetrievalRuntime]:
        @asynccontextmanager
        async def runtime() -> AsyncIterator[RetrievalRuntime]:
            if self.model_error is not None:
                raise self.model_error
            self.opened += 1

            async def count() -> int:
                return self.indexed_documents

            yield RetrievalRuntime(self.indexer, self.searcher, count)

        return runtime()

    def fetch_model(self, model_dir: Path) -> tuple[str, ...]:
        self.fetched.append(model_dir)
        if self.fetch_error is not None:
            raise self.fetch_error
        return self.downloaded

    def wiring(self, settings: Settings | None = None) -> Wiring:
        return Wiring(
            settings=lambda: settings or settings_with(),
            retrieval=self.retrieval,
            fetch_model=self.fetch_model,
        )


def run(
    argv: list[str], harness: Harness | None = None, settings: Settings | None = None
) -> tuple[int, str, str, Harness]:
    harness = harness or Harness()
    return _run(argv, harness, harness.wiring(settings))


def _run(argv: list[str], harness: Harness, wiring: Wiring) -> tuple[int, str, str, Harness]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(argv, wiring)
    return code, out.getvalue(), err.getvalue(), harness


@pytest.fixture
def eval_files(tmp_path: Path) -> list[str]:
    pairs, out_of_scope = tmp_path / "pairs.jsonl", tmp_path / "oos.jsonl"
    pairs.write_text(PAIRS, encoding="utf-8")
    out_of_scope.write_text(OUT_OF_SCOPE, encoding="utf-8")
    return ["--pairs", str(pairs), "--out-of-scope", str(out_of_scope)]


def test_fetch_model_downloads_into_the_configured_folder(tmp_path: Path) -> None:
    harness = Harness(downloaded=("onnx/model.onnx", "onnx/tokenizer.json"))

    code, out, err, _ = run(
        ["retrieval", "fetch-model"], harness, settings_with(retrieval_model_dir=tmp_path)
    )

    assert code == 0
    assert harness.fetched == [tmp_path]
    assert "baixado: onnx/model.onnx" in out
    assert err == ""


def test_fetch_model_says_when_there_was_nothing_to_download() -> None:
    code, out, _, _ = run(["retrieval", "fetch-model"])

    assert code == 0
    assert "nada foi baixado" in out


def test_fetch_model_with_a_file_that_does_not_match_exits_1_naming_the_file() -> None:
    harness = Harness(fetch_error=ModelFileError("model_file.hash_mismatch", "onnx/model.onnx"))

    code, out, err, _ = run(["retrieval", "fetch-model"], harness)

    assert code == 1
    assert out == ""
    assert "onnx/model.onnx" in err
    assert "SHA-256" in err


def test_fetch_model_without_network_exits_2_naming_the_file() -> None:
    harness = Harness(fetch_error=ModelDownloadError("onnx/tokenizer.json"))

    code, _, err, _ = run(["retrieval", "fetch-model"], harness)

    assert code == 2
    assert "onnx/tokenizer.json" in err
    assert "rode de novo" in err


def test_index_reports_what_was_recalculated_kept_and_removed() -> None:
    code, out, err, harness = run(["retrieval", "index"])

    assert code == 0
    assert harness.indexer.runs == 1
    assert "2 documentos recalculados, 1 mantidos, 0 removidos" in out
    assert "recalculados: POL-01, POL-02" in out
    assert err == ""


def test_index_lists_the_removed_documents() -> None:
    harness = Harness(indexer=FakeIndexer(IndexReport((), ("POL-01",), ("POL-12",))))

    _, out, _, _ = run(["retrieval", "index"], harness)

    assert "removidos: POL-12" in out


def test_index_with_a_missing_model_exits_1_with_the_fix() -> None:
    harness = Harness(model_error=ModelFileError("model_file.missing", "onnx/model.onnx"))

    code, _, err, _ = run(["retrieval", "index"], harness)

    assert code == 1
    assert "onnx/model.onnx" in err
    assert "fetch-model" in err


def test_index_with_a_bad_document_exits_1_naming_it() -> None:
    error = ChunkingError("chunking.duplicate_section", "POL-03", "Prazo")
    harness = Harness(indexer=FakeIndexer(error=error))

    code, _, err, _ = run(["retrieval", "index"], harness)

    assert code == 1
    assert "POL-03" in err
    assert "Prazo" in err


@pytest.mark.parametrize("error", [DatabaseUnavailableError(), DatabaseTimeoutError()])
def test_index_with_the_database_down_or_locked_exits_2(error: Exception) -> None:
    harness = Harness(indexer=FakeIndexer(error=error))

    code, _, err, _ = run(["retrieval", "index"], harness)

    assert code == 2
    assert "docker compose up -d" in err


def test_evaluate_prints_the_tables(eval_files: list[str]) -> None:
    code, out, err, harness = run(["retrieval", "evaluate", *eval_files])

    assert code == 0
    assert harness.opened == 1
    assert "2 pares e 1 perguntas fora do escopo" in out
    for fragment in (
        "recall@3",
        "MRR@3",
        "lexical",
        "semântica",
        "híbrida",
        "Maior similaridade semântica",
        "mínimo",
        "mediana",
        "Grade de limiares",
        "0,80",
        "0,90",
        "Latência",
        "p50",
        "p95",
    ):
        assert fragment in out, fragment
    assert "Traceback" not in err


def test_evaluate_marks_the_current_threshold_in_the_candidate_table(
    eval_files: list[str],
) -> None:
    _, out, _, _ = run(
        ["retrieval", "evaluate", *eval_files],
        settings=settings_with(retrieval_min_similarity=0.83),
    )

    marked = [line for line in out.splitlines() if "<- limiar atual" in line]
    assert len(marked) == 1
    assert marked[0].split()[0] == "0,83"
    assert marked[0].endswith("regra similarity_only")


def test_evaluate_prints_both_scores_and_both_rules_of_the_grid(eval_files: list[str]) -> None:
    _, out, _, _ = run(["retrieval", "evaluate", *eval_files])

    assert "Por seção" in out
    assert "Por documento" in out
    assert "(a) nas duas listas + similaridade" in out
    assert "(b) só similaridade" in out
    grid = [line for line in out.splitlines() if line.strip()[:4] in {"0,80", "0,85", "0,90"}]
    assert len(grid) >= 3


def test_evaluate_names_the_configured_rule(eval_files: list[str]) -> None:
    settings = settings_with(retrieval_reliability_rule="both_lists_and_similarity")

    _, out, _, _ = run(["retrieval", "evaluate", *eval_files], settings=settings)

    assert "regra atual ((a) nas duas listas e similaridade)" in out


def test_evaluate_uses_the_versioned_sets_by_default() -> None:
    harness = Harness()

    code, out, _, _ = run(["retrieval", "evaluate"], harness)

    assert code == 0
    assert "60 pares" in out


def test_evaluate_with_an_empty_index_exits_1_and_says_to_index(eval_files: list[str]) -> None:
    harness = Harness(indexed_documents=0)

    code, out, err, _ = run(["retrieval", "evaluate", *eval_files], harness)

    assert code == 1
    assert out == ""
    assert "retrieval index" in err


def test_evaluate_with_a_malformed_file_exits_1_with_the_line(tmp_path: Path) -> None:
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"document": "POL-01", "section": "A", "question": "q"}\nnot json\n')

    code, out, err, harness = run(["retrieval", "evaluate", "--pairs", str(bad)])

    assert code == 1
    assert out == ""
    assert "bad.jsonl, linha 2" in err
    assert harness.opened == 0


def test_evaluate_with_a_missing_file_exits_1(tmp_path: Path) -> None:
    code, _, err, _ = run(["retrieval", "evaluate", "--pairs", str(tmp_path / "absent.jsonl")])

    assert code == 1
    assert "absent.jsonl" in err


def test_evaluate_with_the_database_down_exits_2(eval_files: list[str]) -> None:
    harness = Harness(model_error=DatabaseUnavailableError())

    code, _, err, _ = run(["retrieval", "evaluate", *eval_files], harness)

    assert code == 2
    assert "docker compose up -d" in err


def test_evaluate_with_a_missing_model_exits_1(eval_files: list[str]) -> None:
    harness = Harness(model_error=ModelFileError("model_file.size_mismatch", "onnx/tokenizer.json"))

    code, _, err, _ = run(["retrieval", "evaluate", *eval_files], harness)

    assert code == 1
    assert "onnx/tokenizer.json" in err


@pytest.mark.parametrize(
    "argv",
    [
        ["retrieval"],
        ["retrieval", "nope"],
        ["retrieval", "evaluate", "--pairs"],
        ["retrieval", "evaluate", "--unknown", "x"],
    ],
)
def test_a_wrong_command_line_exits_1(argv: list[str]) -> None:
    code, _, err, harness = run(argv)

    assert code == 1
    assert "erro" in err or "usage" in err
    assert harness.opened == 0
    assert harness.fetched == []


def test_the_evaluation_does_not_flood_the_error_stream_with_one_event_per_search(
    eval_files: list[str],
) -> None:
    _, _, err, _ = run(["retrieval", "evaluate", *eval_files])

    assert "retrieval.search" not in err


async def test_the_production_runtime_refuses_a_model_folder_without_the_files(
    tmp_path: Path,
) -> None:
    with pytest.raises(ModelFileError) as error:
        async with database_retrieval(settings_with(retrieval_model_dir=tmp_path)):
            pass

    assert error.value.rule == "model_file.missing"
