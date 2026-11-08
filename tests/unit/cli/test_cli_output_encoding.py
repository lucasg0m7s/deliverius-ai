"""The operator reads Portuguese: the output is UTF-8 whatever the console code page is."""

import io
import sys

import pytest

from deliverius.cli.datagen import say, use_utf8_output


def test_the_text_goes_out_as_utf8_even_if_the_stream_was_opened_with_another_encoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252", newline="\n")
    monkeypatch.setattr(sys, "stdout", stream)

    use_utf8_output()
    say("atenção: não")
    stream.flush()

    assert raw.getvalue() == b"aten\xc3\xa7\xc3\xa3o: n\xc3\xa3o\n"


def test_a_stream_that_cannot_be_reconfigured_is_left_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "stdout", io.StringIO())

    use_utf8_output()
