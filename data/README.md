# Dados

Tudo aqui é sintético: o Deliverius não existe, e nomes, CPFs, telefones, e-mails e endereços são falsos (Faker
pt-BR). Os conjuntos completos ficam fora do git e são publicados no Hugging Face Datasets, com versão e *dataset card*.
Como foram gerados e o que se sabe dos seus limites está em [`docs/dados-sinteticos.md`](../docs/dados-sinteticos.md).

| Pasta | Conteúdo | No git |
| --- | --- | --- |
| `samples/` | Até 50 linhas sorteadas de cada conjunto da versão 1.0.0, para ver o formato sem baixar nada | Sim |
| `human-test/` | Formato, modelo e validação do conjunto de teste escrito por pessoas | Sim |
| `generated/v<versão>/` | A saída completa de `python -m deliverius.cli datagen all`, com o `manifest.json` | Não |

## Formato

JSONL, um registro por linha, com dinheiro em centavos inteiros e instantes em ISO 8601 UTC. O contrato de cada
registro são os modelos Pydantic de [`src/deliverius/datagen/records.py`](../src/deliverius/datagen/records.py).

| Arquivo da amostra | Registro | Campos principais |
| --- | --- | --- |
| `messages_{train,validation,test}.jsonl` | `MessageRecord` | `text`, `intent` (11 classes), `scenario_id`, `pii_spans` (tipo e posição de cada dado pessoal), `has_injection`, `persona` |
| `fraud_history_{train,test}.jsonl` | `ClaimRecord` | pedido, instante, motivo, itens e valor pedidos, resolução da política, `is_fraud` |
| `policy_qa.jsonl` | `PolicyQaRecord` | `question`, `document` (POL-01 a POL-12), `section` |
| `e2e_scenarios.jsonl` | `E2EScenarioRecord` | `flow` (F1 a F7), estado inicial, objetivo do cliente, estado final esperado |
| `customers.jsonl`, `restaurants.jsonl`, `orders.jsonl` | Mundo simulado | clientes (com dados pessoais falsos), cardápio, pedidos com itens e linha do tempo |

O teste de outro gerador (`messages_holdout`) e o arquivo `fraud_ground_truth` (perfil do cliente e rótulo trocado,
só para análise) existem no conjunto completo.

## Reproduzir

```bash
uv run python -m deliverius.cli datagen all --seed 42
```

Sem LLM, `world` e `scenarios` geram as partes determinísticas em segundos. As mensagens precisam das chaves do Gemini
e do Groq no `.env`; o cache de respostas em `.cache/datagen.sqlite` torna a repetição gratuita e idêntica.
