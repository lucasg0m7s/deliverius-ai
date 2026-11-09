"""What the retrieval commands tell the operator, in Portuguese. Logs stay in English.

Nothing here prints: each function returns the lines, so a test reads them without a terminal.
"""

from collections.abc import Sequence
from typing import Final

from deliverius.retrieval.evaluation import (
    ConfidenceReport,
    Distribution,
    EvaluationReport,
    Interval,
    LatencyReport,
    ModeReport,
    Score,
)
from deliverius.retrieval.indexer import IndexReport

FETCH_MODEL_ADVICE: Final = "Rode `python -m deliverius.cli retrieval fetch-model`."
INDEX_ADVICE: Final = "Rode `python -m deliverius.cli retrieval index`."
DATABASE_UNAVAILABLE: Final = (
    "O banco não respondeu (indisponível ou tempo esgotado). Suba o banco com "
    "`docker compose up -d` e rode de novo."
)
EMPTY_INDEX: Final = f"O índice de políticas está vazio. {INDEX_ADVICE}"
_MODEL_FILE_PROBLEMS: Final = {
    "model_file.missing": "está ausente",
    "model_file.size_mismatch": "tem um tamanho diferente do esperado",
    "model_file.hash_mismatch": "tem um SHA-256 diferente do esperado",
    "model_file.no_pad_token": "não define o token de preenchimento",
}
_CHUNKING_PROBLEMS: Final = {
    "chunking.duplicate_section": "tem duas seções com o mesmo título",
    "chunking.empty_slug": "tem uma seção cujo título não tem letras nem números",
}
_DATA_PROBLEMS: Final = {
    "evaluation_data.unreadable": "o arquivo não pode ser lido",
    "evaluation_data.not_utf8": "o arquivo não está em UTF-8",
    "evaluation_data.empty": "o arquivo está vazio",
    "evaluation_data.invalid_json": "a linha não é um JSON válido",
    "evaluation_data.not_an_object": "a linha não é um objeto JSON",
    "evaluation_data.missing_field": "a linha não tem o campo de texto esperado",
}
_MODE_NAMES: Final = {"lexical": "lexical", "semantic": "semântica", "hybrid": "híbrida"}
_RULE_NAMES: Final = {
    "both_lists_and_similarity": "(a) nas duas listas e similaridade",
    "similarity_only": "(b) só similaridade",
}
_TOLERANCE: Final = 1e-9


def model_file_error(rule: str, file: str) -> str:
    problem = _MODEL_FILE_PROBLEMS.get(rule, f"foi recusado ({rule})")
    return f"O arquivo {file} do modelo {problem}. {FETCH_MODEL_ADVICE}"


def download_failed(file: str) -> str:
    return f"Não foi possível baixar o arquivo {file} do modelo. Confira a conexão e rode de novo."


def chunking_error(rule: str, document: str, section: str) -> str:
    problem = _CHUNKING_PROBLEMS.get(rule, f"foi recusado ({rule})")
    return f"O documento {document} {problem} (seção '{section}')."


def evaluation_data_error(rule: str, file: str, line: int) -> str:
    problem = _DATA_PROBLEMS.get(rule, rule)
    where = f"{file}, linha {line}" if line else file
    return f"Arquivo de avaliação inválido ({where}): {problem}."


def fetch_model_lines(model_dir: str, downloaded: Sequence[str]) -> list[str]:
    if not downloaded:
        return [f"O modelo já está completo em {model_dir}: nada foi baixado."]
    return [
        f"Modelo baixado e conferido em {model_dir}.",
        *[f"  baixado: {file}" for file in downloaded],
    ]


def index_lines(report: IndexReport) -> list[str]:
    lines = [
        f"Indexação concluída: {len(report.recalculated)} documentos recalculados, "
        f"{len(report.kept)} mantidos, {len(report.removed)} removidos."
    ]
    if report.recalculated:
        lines.append(f"  recalculados: {', '.join(report.recalculated)}")
    if report.removed:
        lines.append(f"  removidos: {', '.join(report.removed)}")
    return lines


def evaluation_lines(
    report: EvaluationReport, pairs: int, out_of_scope: int, rule: str, threshold: float
) -> list[str]:
    return [
        f"Avaliação da busca nas políticas: {pairs} pares e {out_of_scope} perguntas fora do "
        "escopo.",
        "",
        *_mode_lines(report.modes),
        "",
        *_confidence_lines(report.confidence, rule, threshold),
        "",
        *_threshold_lines(report.confidence, rule, threshold),
        "",
        *_latency_lines(report.latency),
    ]


def decimal(value: float, digits: int = 3) -> str:
    return f"{value:.{digits}f}".replace(".", ",")


def percent(value: float) -> str:
    return f"{value * 100:.1f}%".replace(".", ",")


def _interval(interval: Interval) -> str:
    return f"[{decimal(interval.low)}; {decimal(interval.high)}]"


def _mode_lines(modes: Sequence[ModeReport]) -> list[str]:
    lines = [
        "Acerto nos 3 primeiros trechos (intervalo de 95% por bootstrap, 1.000 reamostragens).",
    ]
    sections = (
        ("Por seção (documento e seção do par):", [(m.mode, m.by_section) for m in modes]),
        (
            "Por documento (algum trecho é do documento do par):",
            [(m.mode, m.by_document) for m in modes],
        ),
    )
    for title, scores in sections:
        lines += [
            title,
            f"  {'modo':<10} {'recall@3':>8}  {'intervalo':<16} {'MRR@3':>6}  intervalo",
            *[_score_line(_MODE_NAMES[mode], score) for mode, score in scores],
        ]
    return lines


def _score_line(label: str, score: Score) -> str:
    return (
        f"  {label:<10} {decimal(score.recall_at_3):>8}  "
        f"{_interval(score.recall_interval):<16} {decimal(score.mrr):>6}  "
        f"{_interval(score.mrr_interval)}"
    )


def _confidence_lines(confidence: ConfidenceReport, rule: str, threshold: float) -> list[str]:
    return [
        f"Busca híbrida com a regra atual ({_RULE_NAMES[rule]}) e limiar {decimal(threshold, 2)}:",
        f"  não confiável nos pares:                {percent(confidence.pairs_unreliable_rate)}",
        "  não confiável fora do escopo:           "
        f"{percent(confidence.out_of_scope_unreliable_rate)}",
        f"  trecho nas duas listas, nos pares:      {percent(confidence.pairs_in_both_lists)}",
        "  trecho nas duas listas, fora do escopo: "
        f"{percent(confidence.out_of_scope_in_both_lists)}",
        "",
        "Maior similaridade semântica de cada consulta:",
        f"  {'':<14} {'mínimo':>7} {'p5':>7} {'p25':>7} {'mediana':>7} {'máximo':>7}",
        _distribution_line("pares", confidence.pairs_similarity),
        _distribution_line("fora do escopo", confidence.out_of_scope_similarity),
    ]


def _distribution_line(label: str, values: Distribution) -> str:
    cells = (values.minimum, values.p5, values.p25, values.median, values.maximum)
    return f"  {label:<14} " + " ".join(f"{decimal(cell):>7}" for cell in cells)


def _threshold_lines(confidence: ConfidenceReport, rule: str, threshold: float) -> list[str]:
    lines = [
        "Grade de limiares para as duas regras: % dos pares confiáveis / % das fora do escopo",
        "recusadas.",
        f"  {'':>6}  {'(a) nas duas listas + similaridade':^35}  {'(b) só similaridade':^25}",
        f"  {'limiar':>6}  {'pares':>16} {'fora recusadas':>18}"
        f"  {'pares':>10} {'fora recusadas':>14}",
    ]
    for row in confidence.thresholds:
        current = abs(row.threshold - threshold) < _TOLERANCE
        mark = f"  <- limiar atual, regra {rule}" if current else ""
        both, only = row.both_lists_and_similarity, row.similarity_only
        lines.append(
            f"  {decimal(row.threshold, 2):>6}  {percent(both.pairs_reliable):>16} "
            f"{percent(both.out_of_scope_refused):>18}  {percent(only.pairs_reliable):>10} "
            f"{percent(only.out_of_scope_refused):>14}{mark}"
        )
    return lines


def _latency_lines(latency: LatencyReport) -> list[str]:
    return [
        f"Latência da busca completa (embedding e SQL), {latency.queries} consultas, uma por vez:",
        f"  p50: {decimal(latency.p50_ms, 1)} ms    p95: {decimal(latency.p95_ms, 1)} ms",
    ]
