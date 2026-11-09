"""What the fraud commands tell the operator, in Portuguese. Logs stay in English.

Nothing here prints: each function returns the lines, so a test reads them without a terminal.
"""

from collections.abc import Sequence
from typing import Final

FETCH_MODEL_ADVICE: Final = "Rode `python -m deliverius.cli fraud fetch-model`."
_PROBLEMS: Final = {
    "model_file.missing": "está ausente",
    "model_file.size_mismatch": "tem um tamanho diferente do esperado",
    "model_file.hash_mismatch": "tem um SHA-256 diferente do esperado",
    "fraud_model.file_invalid": "não tem o conteúdo esperado",
    "fraud_model.schema_mismatch": "foi treinado com outro esquema de features",
    "fraud_model.feature_names_mismatch": "lista as features em outra ordem",
}


def model_error(rule: str, file: str | None) -> str:
    problem = _PROBLEMS.get(rule, f"foi recusado ({rule})")
    return f"O arquivo {file or '?'} do modelo de fraude {problem}. {FETCH_MODEL_ADVICE}"


def download_failed(file: str) -> str:
    return (
        f"Não foi possível baixar o arquivo {file} do modelo de fraude. "
        "Confira a conexão e rode de novo."
    )


def fetch_model_lines(model_dir: str, downloaded: Sequence[str]) -> list[str]:
    if not downloaded:
        return [f"O modelo de fraude já está completo em {model_dir}: nada foi baixado."]
    return [
        f"Modelo de fraude baixado e conferido em {model_dir}.",
        *[f"  baixado: {file}" for file in downloaded],
    ]
