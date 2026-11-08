# Relatório da change `mascaramento-dados`

## Recall por tipo

Valor inteiro escondido, com 1.000 amostras por tipo, Faker 40.44 pt-BR, semente fixa, em frases curtas
(`tests/support/personal_data_samples.py`).

| Tipo | Recall | Piso em teste |
| --- | --- | --- |
| CPF pontuado | 100% | 99% |
| CPF sem pontuação | 100% | 99% |
| CNPJ | 100% | 99% |
| Cartão | 100% | 99% |
| Telefone | 100% | 99% |
| Celular | 100% | 99% |
| E-mail | 100% | 99% |
| CEP | 100% | 99% |
| Endereço com logradouro comum e número | 100% | 99% |
| Endereço do Faker com número | 37,6% | — |
| Endereço do Faker, qualquer um | 32,2% | — |
| Nome próprio | 0% | — |

**Como ler:** os 100% são um teto. As frases são simples, e os valores vêm no formato que o próprio Faker gera. O
número realista vem do eval de privacidade (changes 6 e 15). O Faker não gera CNPJ alfanumérico; esse formato é
conferido com o exemplo oficial da Receita e com testes do dígito verificador.

**Limite de nomes:** o módulo não reconhece nomes próprios, por isso 0%. O nome que o cliente escrever chega ao LLM,
sem documento nem contato. O eval de privacidade decide se um NER em português entra como melhoria.

## Regressão do logger

Teste escrito antes da correção, rodando contra o `configure_logging` antigo: 4 de 4 falharam (structlog e biblioteca
padrão, em JSON e no console), com o CPF na saída:

```text
E   assert '123.456.789-09' not in '{"event": "...6.789-09"}\n'
FAILED test_logging.py::TestExceptionText::test_standard_library_exception_is_redacted[console]
FAILED test_logging.py::TestExceptionText::test_standard_library_exception_is_redacted[json]
FAILED test_logging.py::TestExceptionText::test_structlog_exception_is_redacted[console]
FAILED test_logging.py::TestExceptionText::test_structlog_exception_is_redacted[json]
```

Depois da correção, os 4 passam, e nenhum teste antigo foi afrouxado.

## Custo do filtro no log

Mediana de `redact_text`, 2.000 execuções, numa máquina de desenvolvimento:

| Texto | Tempo |
| --- | --- |
| Nome de evento curto | 15 µs |
| Campo de frase comum | 28 µs |
| UUID | 32 µs |
| 1.000 caracteres | 413 µs |

Abaixo dos 100 µs por campo definidos na tarefa, então nenhum atalho é necessário.

## Verificações que precisavam falhar

Cada uma foi desfeita em seguida.

| Mudança temporária | Resultado |
| --- | --- |
| Importar `structlog` em `privacy` | `lint-imports`: "privacy is pure" quebrado |
| Desligar o padrão de e-mail | Teste de recall falha: `recall of email fell to 0.00%` |
| Tirar testes de ramo de `documents.py` | Etapa de cobertura falha: 95,65%, abaixo de 100% |

As propriedades do Hypothesis também rodaram uma vez com 20.000 exemplos cada, sem nenhuma falha.
