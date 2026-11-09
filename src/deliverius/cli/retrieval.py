"""`python -m deliverius.cli retrieval <subcommand>`: the policy search from the command line.

Exit codes: 0 done, 1 invalid use, input or model, 2 a dependency did not answer (database, model
download): nothing is lost, run again later. One event loop per run, because the database driver
ties its connections to the loop that opened them.
"""

import argparse
import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Final, Protocol

from deliverius.cli import retrieval_texts as texts
from deliverius.cli.console import EXIT_INVALID, EXIT_OK, complain, configure_cli_logging, say
from deliverius.cli.wiring import Wiring
from deliverius.config import Settings
from deliverius.db.errors import DatabaseUnavailableError
from deliverius.retrieval import (
    ChunkingError,
    EvaluationDataError,
    EvaluationReport,
    IndexReport,
    ModelDownloadError,
    ModelFileError,
    QaPair,
    evaluate,
    load_out_of_scope,
    load_pairs,
)

EXIT_TRY_LATER: Final = 2

DEFAULT_PAIRS: Final = Path("data/eval/policy_qa.jsonl")
DEFAULT_OUT_OF_SCOPE: Final = Path("data/eval/out_of_scope.jsonl")


class _Subparsers(Protocol):
    def add_parser(self, name: str, *, help: str) -> argparse.ArgumentParser: ...


def register(tools: _Subparsers) -> None:
    """Adds the `retrieval` tool to the subparsers of the top-level parser."""
    retrieval = tools.add_parser("retrieval", help="busca nas políticas")
    commands = retrieval.add_subparsers(dest="subcommand", required=True, metavar="subcomando")
    fetch = commands.add_parser("fetch-model", help="baixa e confere o modelo de embedding")
    fetch.set_defaults(run=_fetch_model)
    index = commands.add_parser("index", help="indexa as políticas (só o que mudou)")
    index.set_defaults(run=_index)
    evaluation = commands.add_parser("evaluate", help="mede a qualidade da busca")
    evaluation.add_argument("--pairs", type=Path, default=DEFAULT_PAIRS, help="pares JSONL")
    evaluation.add_argument(
        "--out-of-scope", type=Path, default=DEFAULT_OUT_OF_SCOPE, help="perguntas fora do escopo"
    )
    evaluation.set_defaults(run=_evaluate)


def _fetch_model(args: argparse.Namespace, wiring: Wiring, settings: Settings) -> int:
    try:
        downloaded = wiring.fetch_model(settings.retrieval_model_dir)
    except ModelFileError as error:
        complain(texts.model_file_error(error.rule, error.file))
        return EXIT_INVALID
    except ModelDownloadError as error:
        complain(texts.download_failed(error.file))
        return EXIT_TRY_LATER
    for line in texts.fetch_model_lines(str(settings.retrieval_model_dir), downloaded):
        say(line)
    return EXIT_OK


def _index(args: argparse.Namespace, wiring: Wiring, settings: Settings) -> int:
    try:
        report = asyncio.run(_reindex(wiring, settings))
    except ModelFileError as error:
        complain(texts.model_file_error(error.rule, error.file))
        return EXIT_INVALID
    except ChunkingError as error:
        complain(texts.chunking_error(error.rule, error.document, error.section))
        return EXIT_INVALID
    except DatabaseUnavailableError:
        complain(texts.DATABASE_UNAVAILABLE)
        return EXIT_TRY_LATER
    for line in texts.index_lines(report):
        say(line)
    return EXIT_OK


async def _reindex(wiring: Wiring, settings: Settings) -> IndexReport:
    async with wiring.retrieval(settings) as runtime:
        return await runtime.indexer.reindex()


def _evaluate(args: argparse.Namespace, wiring: Wiring, settings: Settings) -> int:
    # Hundreds of searches would bury the report under one `retrieval.search` line each.
    configure_cli_logging(settings.model_copy(update={"log_level": "WARNING"}))
    try:
        pairs = load_pairs(args.pairs)
        out_of_scope = load_out_of_scope(args.out_of_scope)
    except EvaluationDataError as error:
        complain(texts.evaluation_data_error(error.rule, error.path.name, error.line))
        return EXIT_INVALID
    try:
        report = asyncio.run(_measure(wiring, settings, pairs, out_of_scope))
    except ModelFileError as error:
        complain(texts.model_file_error(error.rule, error.file))
        return EXIT_INVALID
    except DatabaseUnavailableError:
        complain(texts.DATABASE_UNAVAILABLE)
        return EXIT_TRY_LATER
    if report is None:
        complain(texts.EMPTY_INDEX)
        return EXIT_INVALID
    for line in texts.evaluation_lines(
        report,
        len(pairs),
        len(out_of_scope),
        settings.retrieval_reliability_rule,
        settings.retrieval_min_similarity,
    ):
        say(line)
    return EXIT_OK


async def _measure(
    wiring: Wiring, settings: Settings, pairs: Sequence[QaPair], out_of_scope: Sequence[str]
) -> EvaluationReport | None:
    """`None` when nothing is indexed: measuring an empty table would only report zeros."""
    async with wiring.retrieval(settings) as runtime:
        if await runtime.count_indexed_documents() == 0:
            return None
        return await evaluate(runtime.searcher, pairs, out_of_scope)
