"""`python -m deliverius.cli datagen <subcommand>`: the synthetic dataset from the command line.

Exit codes: 0 done, 1 invalid use or input, 2 the LLM budget ran out (nothing written, run again
later: the cache keeps what was done). Every LLM step of a run shares one event loop, because the
HTTP client of some providers is tied to the loop that first used it (`docs/robustez.md`, sec. 9).
"""

import argparse
import asyncio
import re
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Final

import structlog
from pydantic import ValidationError

from deliverius.cli import retrieval, texts
from deliverius.cli.console import (
    EXIT_INVALID,
    EXIT_OK,
    Parser,
    complain,
    configure_cli_logging,
    say,
)
from deliverius.cli.wiring import Wiring
from deliverius.config import Settings
from deliverius.datagen.config import DATAGEN_VERSION, DatagenConfig
from deliverius.datagen.errors import BudgetExhaustedError
from deliverius.datagen.human_test import HumanTestFileError, validate_human_test
from deliverius.datagen.output import DatasetError, write_dataset
from deliverius.datagen.pipeline import (
    ALL_STEPS,
    GeneratedDataset,
    LlmAccess,
    Step,
    estimate_calls,
    llm_access,
    roles_needed,
    run_pipeline,
)
from deliverius.datagen.publish import (
    PublishError,
    ReviewResult,
    announce_dry_run,
    plan_publication,
    publish,
)
from deliverius.datagen.scenarios import ScenarioError
from deliverius.db.errors import DatabaseUnavailableError
from deliverius.llm.errors import LlmConfigurationError
from deliverius.llm.ports import BudgetStatus
from deliverius.llm.roles import ModelRole
from deliverius.llm.runtime import LlmRuntime

logger = structlog.get_logger(__name__)

EXIT_BUDGET_EXHAUSTED: Final = 2

DEFAULT_SEED: Final = 42
DEFAULT_CACHE: Final = Path(".cache/datagen.sqlite")
DEFAULT_CONCURRENCY: Final = 4
GENERATED_ROOT: Final = Path("data/generated")
# `namespace/name`, as the Hub spells it.
_REPO_ID: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}")

_STEPS_OF: Final = {
    "world": (Step.WORLD,),
    "scenarios": (Step.SCENARIOS,),
    "messages": (Step.MESSAGES,),
    "holdout": (Step.HOLDOUT,),
    "rag-pairs": (Step.RAG_PAIRS,),
    "all": ALL_STEPS,
}
_USES_LLM: Final = frozenset({"messages", "holdout", "rag-pairs", "all"})


_NOT_POSITIVE: Final = "deve ser um inteiro maior que zero"
_NOT_A_RATE: Final = "deve ser um número entre 0 e 1"


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(_NOT_POSITIVE) from None
    if value < 1:
        raise argparse.ArgumentTypeError(_NOT_POSITIVE)
    return value


def _rate(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(_NOT_A_RATE) from None
    if not 0.0 <= value <= 1.0:
        raise argparse.ArgumentTypeError(_NOT_A_RATE)
    return value


def default_output(subcommand: str) -> Path:
    if subcommand == "all":
        return GENERATED_ROOT / f"v{DATAGEN_VERSION}"
    return GENERATED_ROOT / "partial" / subcommand


def build_parser() -> argparse.ArgumentParser:
    parser = Parser(prog="python -m deliverius.cli")
    tools = parser.add_subparsers(dest="tool", required=True, metavar="ferramenta")
    datagen = tools.add_parser("datagen", help="dados sintéticos")
    commands = datagen.add_subparsers(dest="subcommand", required=True, metavar="subcomando")
    for name in _STEPS_OF:
        command = commands.add_parser(name, help=f"gera o passo '{name}'")
        command.add_argument("--seed", type=int, default=DEFAULT_SEED)
        command.add_argument(
            "--out", type=Path, default=None, help=f"padrão: {default_output(name)}"
        )
        command.add_argument("--overwrite", action="store_true", help="substitui a pasta de saída")
        if name in _USES_LLM:
            command.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
            command.add_argument("--concurrency", type=_positive_int, default=DEFAULT_CONCURRENCY)
        command.set_defaults(run=_generate)
    validate = commands.add_parser("validate-human", help="valida o teste escrito por pessoas")
    validate.add_argument("file", type=Path, help="arquivo CSV")
    validate.set_defaults(run=_validate_human)
    published = commands.add_parser("publish", help="publica o dataset no Hugging Face")
    published.add_argument("--repo", required=True, help="repositório no formato usuario/nome")
    published.add_argument("--dir", type=Path, default=None, help="pasta gerada por `all`")
    published.add_argument("--yes", action="store_true", help="envia de verdade")
    published.add_argument("--review-defect-rate", type=_rate, default=None, help="de 0 a 1")
    published.add_argument("--review-sample-size", type=_positive_int, default=None)
    published.set_defaults(run=_publish)
    retrieval.register(tools)
    return parser


def main(argv: Sequence[str] | None = None, wiring: Wiring | None = None) -> int:
    wiring = wiring or Wiring()
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as stop:
        return stop.code if isinstance(stop.code, int) else EXIT_INVALID
    try:
        settings = wiring.settings()
    except ValidationError as error:
        # Only the names: the rejected value can be a secret.
        names = sorted({str(problem["loc"][0]).upper() for problem in error.errors()})
        complain(f"Configuração inválida: {', '.join(names)}.")
        return EXIT_INVALID
    configure_cli_logging(settings)
    code: int = args.run(args, wiring, settings)
    return code


def _generate(args: argparse.Namespace, wiring: Wiring, settings: Settings) -> int:
    subcommand: str = args.subcommand
    out: Path = args.out or default_output(subcommand)
    if out.exists() and not args.overwrite:
        complain(f"A pasta {out} já existe. Use --overwrite para substituí-la.")
        return EXIT_INVALID
    config = _config_for(args, wiring.base_config)
    try:
        dataset = asyncio.run(_run(args, wiring, settings, config, out))
    except BudgetExhaustedError as stopped:
        for line in texts.budget_message(stopped):
            complain(line)
        return EXIT_BUDGET_EXHAUSTED
    except LlmConfigurationError as error:
        for line in texts.configuration_message(error.missing_keys):
            complain(line)
        return EXIT_INVALID
    except (DatasetError, ScenarioError) as error:
        complain(f"Não foi possível gerar o conjunto: {error}")
        return EXIT_INVALID
    for line in texts.generation_summary(str(out), dataset):
        say(line)
    return EXIT_OK


def _config_for(args: argparse.Namespace, base: DatagenConfig) -> DatagenConfig:
    messages = base.messages
    if args.subcommand in _USES_LLM:
        messages = messages.model_copy(update={"concurrency": args.concurrency})
    return base.model_copy(update={"seed": args.seed, "messages": messages})


async def _run(
    args: argparse.Namespace,
    wiring: Wiring,
    settings: Settings,
    config: DatagenConfig,
    out: Path,
) -> GeneratedDataset:
    steps = _STEPS_OF[args.subcommand]
    roles = roles_needed(steps)
    commit = wiring.commit()
    if not roles:
        dataset = await run_pipeline(args.subcommand, steps, config, commit)
    else:
        async with wiring.runtime(settings, roles) as runtime:
            access = llm_access(runtime, settings, args.cache)
            await _announce_and_check(runtime, access, config, steps)
            dataset = await run_pipeline(args.subcommand, steps, config, commit, access)
    written = write_dataset(out, dataset.files, dataset.manifest, overwrite=args.overwrite)
    return replace(dataset, manifest=written)


async def _announce_and_check(
    runtime: LlmRuntime, access: LlmAccess, config: DatagenConfig, steps: Sequence[Step]
) -> None:
    """Shows the estimate and today's use, then refuses to start if the budget is already spent."""
    estimate = estimate_calls(config, steps)
    statuses = {role: await _status_or_none(runtime, role) for role in estimate}
    for line in texts.estimate_lines(estimate, access.model_names, statuses):
        say(line)
    for role in estimate:
        reason = await access.guard_for(role)()
        if reason is not None:
            raise BudgetExhaustedError(estimate[role], reason)


async def _status_or_none(runtime: LlmRuntime, role: ModelRole) -> BudgetStatus | None:
    try:
        return await runtime.budget_status(role)
    except DatabaseUnavailableError:
        return None


def _validate_human(args: argparse.Namespace, wiring: Wiring, settings: Settings) -> int:
    try:
        report = validate_human_test(args.file)
    except HumanTestFileError as error:
        complain(texts.human_file_error(error.rule))
        return EXIT_INVALID
    for line in texts.human_test_lines(report):
        (say if report.is_valid else complain)(line)
    return EXIT_OK if report.is_valid else EXIT_INVALID


def _publish(args: argparse.Namespace, wiring: Wiring, settings: Settings) -> int:
    if _REPO_ID.fullmatch(args.repo) is None:
        complain("O repositório deve ter o formato usuario/nome.")
        return EXIT_INVALID
    directory: Path = args.dir or default_output("all")
    review = ReviewResult(args.review_defect_rate, args.review_sample_size)
    try:
        plan = plan_publication(directory, review)
    except DatasetError as error:
        complain(f"O dataset em {directory} não é confiável ou não existe: {error}")
        return EXIT_INVALID
    except PublishError as error:
        complain(texts.publish_error_message(error))
        return EXIT_INVALID
    if not args.yes:
        announce_dry_run(plan, args.repo)
        for line in texts.dry_run_lines(plan, args.repo):
            say(line)
        return EXIT_OK
    try:
        publish(plan, args.repo, settings.hf_token, wiring.hub)
    except PublishError as error:
        complain(texts.publish_error_message(error))
        return EXIT_INVALID
    say(f"Publicado em {args.repo} com a tag {plan.tag}.")
    return EXIT_OK
