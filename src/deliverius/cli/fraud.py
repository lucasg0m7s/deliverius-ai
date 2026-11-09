"""`python -m deliverius.cli fraud <subcommand>`: the fraud model from the command line.

Exit codes: 0 done, 1 a file does not match the pinned model, 2 the hub did not answer: nothing
that passes the check is left behind, run again later.
"""

import argparse
from typing import Final, Protocol

from deliverius.artifacts import ArtifactDownloadError, ArtifactFileError
from deliverius.cli import fraud_texts as texts
from deliverius.cli.console import EXIT_INVALID, EXIT_OK, complain, say
from deliverius.cli.wiring import Wiring
from deliverius.config import Settings
from deliverius.fraud import FraudModelError

EXIT_TRY_LATER: Final = 2


class _Subparsers(Protocol):
    def add_parser(self, name: str, *, help: str) -> argparse.ArgumentParser: ...


def register(tools: _Subparsers) -> None:
    """Adds the `fraud` tool to the subparsers of the top-level parser."""
    fraud = tools.add_parser("fraud", help="modelo antifraude de reembolso")
    commands = fraud.add_subparsers(dest="subcommand", required=True, metavar="subcomando")
    fetch = commands.add_parser("fetch-model", help="baixa e confere o modelo de fraude")
    fetch.set_defaults(run=_fetch_model)


def _fetch_model(args: argparse.Namespace, wiring: Wiring, settings: Settings) -> int:
    try:
        downloaded = wiring.fetch_fraud_model(settings.fraud_model_dir)
    except (ArtifactFileError, FraudModelError) as error:
        complain(texts.model_error(error.rule, error.file))
        return EXIT_INVALID
    except ArtifactDownloadError as error:
        complain(texts.download_failed(error.file))
        return EXIT_TRY_LATER
    for line in texts.fetch_model_lines(str(settings.fraud_model_dir), downloaded):
        say(line)
    return EXIT_OK
