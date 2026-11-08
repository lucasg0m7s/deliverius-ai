"""What the commands tell the operator, in Portuguese. Logs stay in English, in the modules.

Nothing here prints: each function returns the lines, so a test reads them without a terminal.
Nothing here quotes the content of a file the operator gave, only line numbers and rules.
"""

from collections.abc import Mapping
from typing import Final

from deliverius.datagen.errors import BudgetExhaustedError
from deliverius.datagen.human_test import HumanTestReport, RowProblem
from deliverius.datagen.pipeline import GeneratedDataset
from deliverius.datagen.publish import TOKEN_VARIABLE, PublishError, PublishPlan
from deliverius.datagen.records import Intent
from deliverius.llm.call import DegradedReason
from deliverius.llm.ports import BudgetStatus
from deliverius.llm.roles import ModelRole

MAX_REPORTED_ISSUES: Final = 200
_RESUME_ADVICE: Final = (
    "Rode de novo depois que o orçamento renovar; o cache guarda o que já foi feito."
)
_BUDGET_REASONS: Final = {
    DegradedReason.BUDGET_EXHAUSTED: "O orçamento diário de chamadas ao LLM acabou.",
    DegradedReason.BUDGET_UNAVAILABLE: (
        "Não foi possível consultar o orçamento (banco indisponível); suba o banco com "
        "`docker compose up -d`."
    ),
    DegradedReason.PROVIDER_PAUSED: "O provedor pediu uma pausa (limite de uso por minuto).",
}
_ROW_PROBLEMS: Final = {
    RowProblem.WRONG_COLUMN_COUNT: "número de colunas diferente do cabeçalho",
    RowProblem.EMPTY_ID: "id vazio",
    RowProblem.DUPLICATE_ID: "id repetido",
    RowProblem.EXAMPLE_ROW: "linha de exemplo (id começa com 'exemplo-')",
    RowProblem.EMPTY_TEXT: "texto vazio",
    RowProblem.TEXT_TOO_LONG: "texto acima do tamanho máximo",
    RowProblem.UNKNOWN_INTENT: "intenção desconhecida",
    RowProblem.PERSONAL_DATA: "dado pessoal detectado",
}
_FILE_ERRORS: Final = {
    "human_test.unreadable": "o arquivo não pode ser lido",
    "human_test.file_too_large": "o arquivo passa do tamanho máximo (1 MB)",
    "human_test.not_utf8": "o arquivo não está em UTF-8",
    "human_test.malformed_csv": "o CSV está malformado",
    "human_test.empty_file": "o arquivo está vazio",
    "human_test.invalid_header": "o cabeçalho deve ter id, text e intent (notes é opcional)",
    "human_test.too_many_rows": "o arquivo passa do máximo de 2.000 linhas",
}
_PUBLISH_ERRORS: Final = {
    "publish.token_missing": (
        f"Falta a variável {TOKEN_VARIABLE} (token do Hugging Face com permissão de escrita). "
        "Nada foi enviado."
    ),
    "publish.tag_exists": "A versão já foi publicada neste repositório; suba o DATAGEN_VERSION.",
    "publish.hub_failed": "O Hugging Face recusou ou não respondeu à chamada.",
    "publish.card_name_taken": "O conjunto já tem um arquivo com o nome do card.",
}


def number(value: int) -> str:
    return f"{value:,}".replace(",", ".")


def estimate_lines(
    estimate: Mapping[ModelRole, int],
    models: Mapping[ModelRole, str],
    statuses: Mapping[ModelRole, BudgetStatus | None],
) -> list[str]:
    """The calls each role will make at least, and what its provider has left today."""
    if not estimate:
        return ["Este passo não chama nenhum LLM."]
    lines = ["Estimativa de chamadas ao LLM (no mínimo; cada nova tentativa conta mais uma):"]
    for role, calls in estimate.items():
        lines.append(f"  {models[role]} (papel {role.value}): {number(calls)} chamadas")
        status = statuses.get(role)
        lines.append(_usage_line(status))
        if status is not None and status.limit.daily_requests is not None:
            left = max(status.limit.daily_requests - status.usage.requests_used, 0)
            if calls > left:
                lines.append(
                    f"    Não cabe no que resta hoje ({number(left)} chamadas): a execução para "
                    "quando o orçamento acabar e pode ser retomada. " + _RESUME_ADVICE
                )
    return lines


def _usage_line(status: BudgetStatus | None) -> str:
    if status is None:
        return "    uso de hoje: indisponível (banco fora do ar)"
    limit = status.limit
    requests = f"{number(status.usage.requests_used)} de {_limit_text(limit.daily_requests)}"
    tokens = f"{number(status.usage.tokens_used)} de {_limit_text(limit.daily_tokens)}"
    return f"    uso de hoje em {status.provider}: {requests} chamadas; {tokens} tokens"


def _limit_text(limit: int | None) -> str:
    return "sem limite" if limit is None else number(limit)


def budget_message(stopped: BudgetExhaustedError) -> list[str]:
    reason = _BUDGET_REASONS.get(stopped.reason, "A execução foi interrompida.")
    return [
        f"{reason} Faltam {number(stopped.remaining)} chamadas para terminar o passo em "
        "andamento. Nada foi gravado.",
        _RESUME_ADVICE,
    ]


def configuration_message(missing_keys: Mapping[str, list[str]]) -> list[str]:
    return [
        f"Falta a chave de API {key} (usada por {', '.join(users)})."
        for key, users in sorted(missing_keys.items())
    ]


def generation_summary(destination: str, dataset: GeneratedDataset) -> list[str]:
    manifest = dataset.manifest
    commit = manifest.commit + (" (com alterações não commitadas)" if manifest.dirty else "")
    lines = [
        f"Gerado em {destination}: {len(manifest.files)} arquivos, versão "
        f"{manifest.datagen_version}, semente {manifest.seed}, commit {commit}."
    ]
    lines += [f"  {name}: {number(total)}" for name, total in manifest.counts.items()]
    if dataset.discard_total:
        lines.append(f"  variações descartadas: {number(dataset.discard_total)}")
    return lines


def human_file_error(rule: str) -> str:
    return f"Arquivo inválido: {_FILE_ERRORS.get(rule, rule)}."


def human_test_lines(report: HumanTestReport) -> list[str]:
    if report.is_valid:
        return [f"Arquivo válido: {number(report.rows)} linhas.", *_intent_lines(report)]
    lines = [
        f"Linha {issue.line}: {_ROW_PROBLEMS[issue.problem]}"
        + ("" if issue.detail is None else f" ({issue.detail})")
        for issue in sorted(report.issues, key=lambda issue: issue.line)[:MAX_REPORTED_ISSUES]
    ]
    hidden = len(report.issues) - MAX_REPORTED_ISSUES
    if hidden > 0:
        lines.append(f"... e mais {number(hidden)} problemas.")
    lines.append(f"{number(report.invalid_rows)} de {number(report.rows)} linhas com problema.")
    return lines


def _intent_lines(report: HumanTestReport) -> list[str]:
    missing = [intent.value for intent in Intent if report.intent_counts.get(intent.value, 0) == 0]
    lines = [f"  {name}: {total}" for name, total in sorted(report.intent_counts.items())]
    if missing:
        lines.append(f"Atenção: nenhuma linha para {', '.join(missing)}.")
    return lines


def dry_run_lines(plan: PublishPlan, repo_id: str) -> list[str]:
    lines = [f"Nada será enviado sem --yes. Seria publicado em {repo_id} com a tag {plan.tag}:"]
    lines += [f"  {file.name} ({number(file.size_bytes)} bytes)" for file in plan.files]
    lines += ["  README.md (o card abaixo)", "", plan.card.rstrip(), ""]
    lines.append("Nada foi enviado. Confira o card e rode de novo com --yes para publicar.")
    return lines


def publish_error_message(error: PublishError) -> str:
    text = _PUBLISH_ERRORS.get(error.rule, f"Falha ao publicar ({error.rule}).")
    detail = error.detail
    if detail is not None and error.rule in {"publish.hub_failed", "publish.tag_exists"}:
        return f"{text} ({detail})"
    return text
