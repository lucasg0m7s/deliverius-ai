"""The test written by people: every rule of the format, and no content of a row in the output."""

import csv
import io
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from deliverius.cli.datagen import main
from deliverius.cli.wiring import Wiring
from deliverius.datagen.human_test import (
    MAX_FILE_BYTES,
    MAX_ROWS,
    MAX_TEXT_CHARS,
    HumanTestFileError,
    RowIssue,
    RowProblem,
    validate_human_test,
)
from deliverius.datagen.records import Intent

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
HUMAN_TEST_FOLDER = REPOSITORY_ROOT / "data" / "human-test"
# A CPF with valid check digits: it must be found, and never echoed.
VALID_CPF = "529.982.247-25"
HEADER = ["id", "text", "intent", "notes"]


def write_csv(path: Path, rows: list[list[str]], header: list[str] | None = None) -> Path:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header if header is not None else HEADER)
    writer.writerows(rows)
    path.write_text(buffer.getvalue(), encoding="utf-8", newline="")
    return path


def problems(path: Path) -> list[tuple[int, RowProblem]]:
    return [(issue.line, issue.problem) for issue in validate_human_test(path).issues]


def test_a_file_that_follows_the_format_is_valid_and_counts_the_intents(tmp_path: Path) -> None:
    path = write_csv(
        tmp_path / "ok.csv",
        [
            ["h-1", "Faltou o refrigerante no meu pedido.", "item_missing", ""],
            ["h-2", "Chegou frio, que decepção", "quality_issue", "tom irritado"],
            ["h-3", "Bom dia!", "other", ""],
        ],
    )

    report = validate_human_test(path)

    assert report.is_valid
    assert report.rows == 3
    assert report.intent_counts == {"item_missing": 1, "quality_issue": 1, "other": 1}


def test_the_notes_column_is_optional(tmp_path: Path) -> None:
    path = write_csv(
        tmp_path / "ok.csv",
        [["h-1", "Cadê meu pedido?", "late_delivery"]],
        ["id", "text", "intent"],
    )

    assert validate_human_test(path).is_valid


def test_an_unknown_intent_points_at_the_line(tmp_path: Path) -> None:
    path = write_csv(
        tmp_path / "bad.csv",
        [["h-1", "ok", "other", ""], ["h-2", "Quero meu dinheiro", "refund_please", ""]],
    )

    assert problems(path) == [(3, RowProblem.UNKNOWN_INTENT)]


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        (["h-1", "", "other", ""], RowProblem.EMPTY_TEXT),
        (["h-1", "   ", "other", ""], RowProblem.EMPTY_TEXT),
        (["h-1", "a" * (MAX_TEXT_CHARS + 1), "other", ""], RowProblem.TEXT_TOO_LONG),
        (["", "texto", "other", ""], RowProblem.EMPTY_ID),
        (["exemplo-1", "texto", "other", ""], RowProblem.EXAMPLE_ROW),
        (["EXEMPLO-9", "texto", "other", ""], RowProblem.EXAMPLE_ROW),
        (["h-1", "texto", "", ""], RowProblem.UNKNOWN_INTENT),
        (["h-1", "texto", "Other", ""], RowProblem.UNKNOWN_INTENT),
        (["h-1", "texto", "other"], RowProblem.WRONG_COLUMN_COUNT),
        (["h-1", "texto", "other", "", "extra"], RowProblem.WRONG_COLUMN_COUNT),
    ],
)
def test_each_rule_of_the_row_is_checked(
    tmp_path: Path, row: list[str], expected: RowProblem
) -> None:
    assert problems(write_csv(tmp_path / "f.csv", [row])) == [(2, expected)]


def test_a_text_of_exactly_the_maximum_is_accepted(tmp_path: Path) -> None:
    path = write_csv(tmp_path / "f.csv", [["h-1", "a" * MAX_TEXT_CHARS, "other", ""]])

    assert validate_human_test(path).is_valid


def test_a_repeated_id_is_flagged_on_the_second_row_only(tmp_path: Path) -> None:
    path = write_csv(
        tmp_path / "f.csv",
        [["h-1", "um", "other", ""], ["h-2", "dois", "other", ""], ["h-1", "três", "other", ""]],
    )

    assert problems(path) == [(4, RowProblem.DUPLICATE_ID)]


def test_a_record_with_a_line_break_reports_the_line_where_it_starts(tmp_path: Path) -> None:
    path = write_csv(
        tmp_path / "f.csv",
        [["h-1", "primeira\nlinha\nda mensagem", "other", ""], ["h-2", "", "other", ""]],
    )

    assert problems(path) == [(5, RowProblem.EMPTY_TEXT)]


def test_a_blank_line_is_not_a_row(tmp_path: Path) -> None:
    path = tmp_path / "f.csv"
    path.write_text("id,text,intent,notes\n\nh-1,oi,other,\n", encoding="utf-8")

    assert validate_human_test(path).is_valid


def test_a_cpf_is_found_and_the_report_never_holds_the_text(tmp_path: Path) -> None:
    secret_text = f"Meu CPF é {VALID_CPF}, pode conferir"
    path = write_csv(tmp_path / "f.csv", [["h-secret-id", secret_text, "other", ""]])

    report = validate_human_test(path)

    assert [(i.line, i.problem, i.detail) for i in report.issues] == [
        (2, RowProblem.PERSONAL_DATA, "CPF")
    ]
    assert VALID_CPF not in repr(report)
    assert "h-secret-id" not in repr(report)


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("me liga no (11) 91234-5678", "TELEFONE"),
        ("escreve pra maria.silva@example.com", "EMAIL"),
        ("mora no CEP 01310-100", "CEP"),
        ("meu cpf é 5٢٩.982.247-25 ok", "CPF"),
    ],
)
def test_other_personal_data_is_found_too(tmp_path: Path, text: str, kind: str) -> None:
    path = write_csv(tmp_path / "f.csv", [["h-1", text, "other", ""]])

    [issue] = validate_human_test(path).issues

    assert issue.problem is RowProblem.PERSONAL_DATA
    assert kind in (issue.detail or "")


def test_personal_data_in_the_notes_counts_too(tmp_path: Path) -> None:
    path = write_csv(tmp_path / "f.csv", [["h-1", "oi", "other", f"cliente {VALID_CPF}"]])

    assert problems(path) == [(2, RowProblem.PERSONAL_DATA)]


def test_a_row_with_several_problems_lists_each(tmp_path: Path) -> None:
    path = write_csv(tmp_path / "f.csv", [["", "", "nope", ""]])

    assert {p for _, p in problems(path)} == {
        RowProblem.EMPTY_ID,
        RowProblem.EMPTY_TEXT,
        RowProblem.UNKNOWN_INTENT,
    }
    assert validate_human_test(path).invalid_rows == 1


@pytest.mark.parametrize(
    "header",
    [
        ["id", "text"],
        ["id", "text", "intent", "intent"],
        ["id", "texto", "intent"],
        ["id", "text", "intent", "notes", "extra"],
    ],
)
def test_a_wrong_header_makes_the_file_unreadable(tmp_path: Path, header: list[str]) -> None:
    path = write_csv(tmp_path / "f.csv", [], header)

    with pytest.raises(HumanTestFileError) as error:
        validate_human_test(path)

    assert error.value.rule == "human_test.invalid_header"


def test_a_file_with_the_columns_in_another_order_is_read_by_name(tmp_path: Path) -> None:
    path = write_csv(tmp_path / "f.csv", [["other", "oi", "h-1"]], ["intent", "text", "id"])

    assert validate_human_test(path).is_valid


def test_a_file_over_the_size_limit_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "big.csv"
    path.write_bytes(b"id,text,intent\n" + b"x" * MAX_FILE_BYTES)

    with pytest.raises(HumanTestFileError) as error:
        validate_human_test(path)

    assert error.value.rule == "human_test.file_too_large"


def test_a_file_with_too_many_rows_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "many.csv"
    path.write_text("id,text,intent\n" + "h,a,other\n" * (MAX_ROWS + 1), encoding="utf-8")

    with pytest.raises(HumanTestFileError) as error:
        validate_human_test(path)

    assert error.value.rule == "human_test.too_many_rows"


def test_the_maximum_number_of_rows_is_accepted(tmp_path: Path) -> None:
    rows = [[f"h-{n}", "oi", "other", ""] for n in range(MAX_ROWS)]

    assert validate_human_test(write_csv(tmp_path / "f.csv", rows)).rows == MAX_ROWS


@pytest.mark.parametrize(
    ("content", "rule"),
    [
        (b"", "human_test.empty_file"),
        (b"\xff\xfe\x00bad", "human_test.not_utf8"),
        (b'id,text,intent\nh-1,"unclosed\n', "human_test.malformed_csv"),
        (b"id,text,intent\nh-1," + b"a" * 200_000 + b",other\n", "human_test.malformed_csv"),
    ],
)
def test_content_that_is_not_a_table_is_refused(tmp_path: Path, content: bytes, rule: str) -> None:
    path = tmp_path / "f.csv"
    path.write_bytes(content)

    with pytest.raises(HumanTestFileError) as error:
        validate_human_test(path)

    assert error.value.rule == rule


def test_a_missing_file_is_unreadable(tmp_path: Path) -> None:
    with pytest.raises(HumanTestFileError) as error:
        validate_human_test(tmp_path / "nope.csv")

    assert error.value.rule == "human_test.unreadable"


def test_a_byte_order_mark_from_a_spreadsheet_is_ignored(tmp_path: Path) -> None:
    path = tmp_path / "f.csv"
    path.write_bytes("id,text,intent\nh-1,oi,other\n".encode("utf-8-sig"))

    assert validate_human_test(path).is_valid


def test_the_template_is_only_a_header_and_the_example_is_refused_as_a_final_file() -> None:
    template = validate_human_test(HUMAN_TEST_FOLDER / "template.csv")
    example = validate_human_test(HUMAN_TEST_FOLDER / "exemplo.csv")

    assert (template.rows, template.is_valid) == (0, True)
    assert example.rows == 2
    assert {issue.problem for issue in example.issues} == {RowProblem.EXAMPLE_ROW}


def test_the_readme_defines_every_intent_and_the_limits() -> None:
    readme = (HUMAN_TEST_FOLDER / "README.md").read_text(encoding="utf-8")

    assert all(f"`{intent.value}`" in readme for intent in Intent)
    for limit in (str(MAX_TEXT_CHARS), "2.000", "1 MB", "UTF-8"):
        assert limit in readme


def test_validating_logs_counts_only(tmp_path: Path) -> None:
    path = write_csv(tmp_path / "f.csv", [["h-1", f"cpf {VALID_CPF}", "other", ""]])

    with capture_logs() as logs:
        validate_human_test(path)

    assert logs == [
        {"event": "datagen.human_test_validated", "rows": 1, "invalid_rows": 1, "log_level": "info"}
    ]


def run_command(path: Path) -> int:
    return main(["datagen", "validate-human", str(path)], Wiring())


def test_the_command_exits_with_0_for_a_valid_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write_csv(tmp_path / "f.csv", [["h-1", "oi", "other", ""]])

    assert run_command(path) == 0
    assert "Arquivo válido: 1 linhas." in capsys.readouterr().out


def test_the_command_names_the_line_and_the_reason_of_an_unknown_intent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write_csv(tmp_path / "f.csv", [["h-1", "Quero dinheiro", "refund_please", ""]])

    assert run_command(path) == 1
    err = capsys.readouterr().err
    assert "Linha 2: intenção desconhecida" in err
    assert "refund_please" not in err
    assert "Quero dinheiro" not in err


def test_the_command_never_prints_the_row_that_holds_a_cpf(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write_csv(
        tmp_path / "f.csv", [["id-4f9a", f"Meu CPF é {VALID_CPF} 4f9a", "bogus-4f9a", "n-4f9a"]]
    )

    assert run_command(path) == 1
    captured = capsys.readouterr()
    everything = captured.out + captured.err
    assert "Linha 2: dado pessoal detectado (CPF)" in captured.err
    assert VALID_CPF not in everything
    assert "4f9a" not in everything


def test_the_command_exits_with_1_for_a_file_it_cannot_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_command(tmp_path / "missing.csv") == 1
    assert "Arquivo inválido: o arquivo não pode ser lido" in capsys.readouterr().err


def test_the_command_lists_every_invalid_row_and_the_total(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write_csv(
        tmp_path / "f.csv",
        [["a", "", "other", ""], ["b", "ok", "other", ""], ["c", "ok", "zzz", ""]],
    )

    assert run_command(path) == 1
    err = capsys.readouterr().err
    assert "Linha 2: texto vazio" in err
    assert "Linha 4: intenção desconhecida" in err
    assert "2 de 3 linhas com problema." in err


def test_an_issue_carries_no_field_that_could_hold_the_text() -> None:
    assert set(RowIssue.__dataclass_fields__) == {"line", "problem", "detail"}
