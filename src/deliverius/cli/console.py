"""What every command shares: the exit codes, the output streams and the argument parser."""

import argparse
import io
import logging
import sys
from typing import Final, NoReturn

from deliverius.config import Settings
from deliverius.observability.logging import configure_logging

EXIT_OK: Final = 0
EXIT_INVALID: Final = 1


def use_utf8_output() -> None:
    """The text is Portuguese: on Windows a pipe or file would otherwise get the ANSI code page."""
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8")


def say(line: str = "") -> None:
    sys.stdout.write(f"{line}\n")


def complain(line: str) -> None:
    sys.stderr.write(f"{line}\n")


class Parser(argparse.ArgumentParser):
    """Exit code 2 means "try again later", so a usage error exits with 1."""

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(EXIT_INVALID, f"{self.prog}: erro: {message}\n")


def configure_cli_logging(settings: Settings) -> None:
    """Logs go to stderr, so stdout holds only what the operator is meant to read."""
    configure_logging(settings)
    for handler in logging.getLogger().handlers:
        if isinstance(handler, logging.StreamHandler):
            handler.setStream(sys.stderr)
