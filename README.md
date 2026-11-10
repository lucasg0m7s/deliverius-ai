# Deliverius AI

[![CI](https://github.com/lucasg0m7s/deliverius-ai/actions/workflows/ci.yml/badge.svg)](https://github.com/lucasg0m7s/deliverius-ai/actions/workflows/ci.yml)
![Python 3.13](https://img.shields.io/badge/python-3.13-blue)
![mypy strict](https://img.shields.io/badge/mypy-strict-informational)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**Deli** é o agente de atendimento com IA do Deliverius, um app de delivery fictício. Quando um cliente escreve
"meu pedido chegou sem o refrigerante", o Deli entende o problema, consulta o pedido, aplica a política da empresa e
resolve: reembolsa, emite cupom, cancela ou chama um humano para aprovar. Tudo com custo, latência e qualidade
medidos.

> Projeto de portfólio em desenvolvimento. O Deliverius não existe: dados, políticas e clientes são sintéticos.

**A ideia central: o LLM propõe, o código decide.** O modelo entende a conversa e sugere uma ação; quem decide se ela
pode sair é um motor de política determinístico e um antifraude treinado. Nenhum valor de reembolso depende de o LLM
obedecer.

```mermaid
flowchart LR
    M[Mensagem do cliente] --> P[Mascaramento<br/>de dados pessoais]
    P --> C[Classificador de intenção<br/>BERTimbau ajustado]
    C --> A[Agente LangGraph<br/>só leitura: pedido, entrega,<br/>histórico, política via RAG]
    A --> X[Proposta de ação<br/>saída estruturada]
    X --> R[Motor de política<br/>regras e limites em código]
    R --> F[Antifraude<br/>regressão logística calibrada,<br/>revisão por custo]
    F -->|risco baixo| E[Executa<br/>idempotente]
    F -->|risco alto| H[Pausa para<br/>aprovação humana]
    E --> V[Resposta validada<br/>com citação]
    H --> V
```

## O que olhar primeiro

| Parte | Por que é interessante | Onde |
| --- | --- | --- |
| **Motor de política** | Regras de reembolso, cupom e cancelamento em código puro, com os números numa fonte única que também preenche os documentos citados pelo RAG: o texto nunca contradiz a regra. 100% dos ramos cobertos | [`src/deliverius/policy/`](src/deliverius/policy/), [`docs/modelo-dominio.md`](docs/modelo-dominio.md#3-a-política-do-deliverius) |
| **Mascaramento (LGPD)** | CPF, CNPJ (inclusive o alfanumérico), cartão, telefone, e-mail, CEP e endereço mascarados antes de qualquer LLM ou log, com recall medido por tipo e limites publicados | [`src/deliverius/privacy/`](src/deliverius/privacy/), [`docs/seguranca.md`](docs/seguranca.md#4-mascaramento-de-dados-pessoais-privacy) |
| **Camada de LLM** | Papéis por tarefa, provedores trocáveis (Gemini, Groq, Ollama), orçamento diário com reserva atômica no Postgres, pausa depois de um `429`, modo degradado tipado e custo por chamada | [`src/deliverius/llm/`](src/deliverius/llm/), [`docs/arquitetura.md`](docs/arquitetura.md#camada-de-llm) |
| **Busca nas políticas (RAG)** | Full-text em português sem acento e embeddings `multilingual-e5-small` em ONNX (sem PyTorch), fundidos por RRF em SQL, com regra de "busca não confiável" para o agente não inventar regra. Porta de qualidade no CI | [`src/deliverius/retrieval/`](src/deliverius/retrieval/), [`docs/busca-politicas.md`](docs/busca-politicas.md) |
| **Antifraude** | Features no ponto do tempo com um só código para treino e aplicação (provado por teste de propriedade e de paridade com o Postgres), regra de escolha escrita antes de rodar (a regressão logística venceu o LightGBM), calibração aplicada sem scikit-learn, revisão humana por custo esperado e porta no CI | [`src/deliverius/fraud/`](src/deliverius/fraud/), [`research/fraud/`](research/fraud/), [`docs/antifraude.md`](docs/antifraude.md) |
| **Dados sintéticos** | O cenário vem primeiro e o rótulo é verdade de construção: o LLM só escreve o texto, com marcadores que o código preenche. Reprodutível por semente e cache, com divisões testadas contra vazamento | [`src/deliverius/datagen/`](src/deliverius/datagen/), [`docs/modelo-dominio.md`](docs/modelo-dominio.md#4-gerador-de-dados-sintéticos-datagen) |
| **Decisões** | Cada decisão de arquitetura com as alternativas consideradas e o porquê | [`docs/decisoes-arquitetura.md`](docs/decisoes-arquitetura.md) |

## Números até aqui

| O quê | Resultado |
| --- | --- |
| Testes | 2.013 unitários (95% de cobertura) e 310 de integração contra Postgres real (Testcontainers) |
| Motor de política, mascaramento, ações e features do antifraude | 100% de cobertura de ramos, exigida no CI |
| Recall do mascaramento | Acima de 99% nos tipos estruturados em dados sintéticos; nomes próprios ainda não reconhecidos (limite escrito) |
| Custo de LLM | Chamada real com ferramenta: US$ 0,000079 no Gemini e US$ 0,000048 no Groq (preço de lista equivalente; o projeto roda na cota gratuita), latência de 0,7 s e 0,3 a 0,5 s |
| Dataset sintético | 2.969 mensagens rotuladas em 11 intenções, 550 de teste de outro gerador, 20.000 pedidos com 3.378 reclamações rotuladas para o antifraude (11,8% de fraude), 60 pares pergunta → política, 150 cenários de ponta a ponta; 5% de rótulo errado numa revisão à mão de 100 mensagens. Publicado em [huggingface.co/datasets/G0m7s/deliverius-ai](https://huggingface.co/datasets/G0m7s/deliverius-ai) (tag `v1.0.0`, CC BY 4.0) |

## Antifraude: por que a regressão logística?

Reclamações de outubro (462, com 51 fraudes), tocadas uma vez, depois da escolha feita só com setembro:

| Modelo | PR-AUC [IC 95%] | Brier | Custo por reclamação |
| --- | --- | --- | --- |
| **Regressão logística calibrada** | **0,452 [0,348; 0,596]** | **0,077** | **R$ 1,25** (revisa quando p × valor ≥ R$ 4) |
| LightGBM calibrado | 0,372 [0,272; 0,507] | 0,084 | — |
| Regra de negócio (3+ reclamações em 30 dias) | 0,123 [0,110; 0,167] | — | R$ 3,14 |
| Sem antifraude | — | — | R$ 3,22 |

A regra de escolha foi escrita antes de rodar: o LightGBM só entraria se vencesse a logística com o intervalo de 95%
da diferença todo acima de zero. Em setembro ele perdeu ([−0,18; −0,01]), e o modelo simples ficou. A aplicação
aplica os coeficientes e o calibrador publicados com numpy e reproduz o scikit-learn até 2e-16. Modelo publicado
em [huggingface.co/G0m7s/deliverius-fraud](https://huggingface.co/G0m7s/deliverius-fraud) (revisão fixa no código,
SHA-256 de cada arquivo conferido, sem *pickle*). Relatório com curvas, SHAP e limites em
[`docs/antifraude.md`](docs/antifraude.md).

## Busca nas políticas: por que híbrida?

60 perguntas → seção da política, acerto nos 3 primeiros trechos (intervalo de 95% por bootstrap):

| Modo | recall@3 (seção) | MRR@3 (seção) | recall@3 (documento) |
| --- | --- | --- | --- |
| Lexical (full-text `pt_unaccent`) | 0,35 [0,22; 0,47] | 0,21 | 0,52 |
| Semântica (e5-small, ONNX) | 0,55 [0,43; 0,67] | 0,43 | 0,70 |
| Híbrida (RRF) | 0,55 [0,42; 0,68] | 0,41 | 0,72 |

Latência p95 de 29 ms em CPU. Com 60 pares, a híbrida não se mostrou melhor que a semântica com significância; fica
pelos termos exatos e números que a semântica dilui, e o relatório mostra por que boa parte dos erros por seção é
ambiguidade de rótulo. A regra de busca não confiável recusa 63% das perguntas fora do escopo e mantém 98% das
perguntas da política. Detalhes, achados e limites em [`docs/busca-politicas.md`](docs/busca-politicas.md).

## Status

O plano completo, change por change, está em [`docs/roadmap.md`](docs/roadmap.md). Cada change nasce como proposta no
[OpenSpec](https://github.com/Fission-AI/OpenSpec) (`openspec/changes/`) e é arquivada só com a verificação verde
(`openspec/changes/archive/`).

| # | Change | Entrega | Status |
| --- | --- | --- | --- |
| 1 | `fundacao-projeto` | Python 3.13, uv, FastAPI, Postgres + pgvector, Docker sem root, CI | ✅ |
| 2 | `nucleo-dominio` | Pedidos, linha do tempo, escopo de acesso no `WHERE`, auditoria só de inserção | ✅ |
| 3 | `motor-politica` | Motor de política puro e 12 documentos com números de fonte única | ✅ |
| 4 | `mascaramento-dados` | Detecção e mascaramento de dados pessoais com marcadores estáveis | ✅ |
| 5 | `camada-llm` | Papéis, provedores, orçamento, registro de uso e custo, cache, modelo falso | ✅ |
| 6 | `gerador-dados` | Dados sintéticos rotulados por construção, CLI, publicação no Hugging Face | ✅ |
| 8 | `busca-politicas` | Busca híbrida nas políticas com avaliação e porta no CI | ✅ |
| 10 | `antifraude-reembolso` | Features no ponto do tempo, regressão logística calibrada contra LightGBM e regra de negócio, revisão por custo, modelo publicado com revisão fixa e porta no CI | ✅ |
| 11 | `acoes-e-aprovacoes` | Ações idempotentes com a política reavaliada sob trava, aprovação humana com trava otimista, transferência para humano | ✅ |
| 7, 9 | Modelos | Pesquisa de intenção (4 abordagens), classificador servido | ⏳ |
| 12–14 | Agente e API | Agente LangGraph, API, observabilidade | ⏳ |
| 15–17 | Avaliação | Evals com clientes simulados e de segurança, juiz calibrado | ⏳ |
| Extras | — | Demo pública, LoRA, servidor MCP, automação n8n | ⏳ |

## Rodando localmente

Pré-requisitos: [uv](https://docs.astral.sh/uv/), que instala o Python 3.13, e Docker.

```bash
cp .env.example .env
uv sync --group research             # o grupo `research` (pesquisa e porta do antifraude) fica fora da imagem
docker compose up -d                 # Postgres 17 + pgvector na porta 5433
uv run alembic upgrade head
uv run fastapi dev src/deliverius/api/main.py   # http://localhost:8000/docs
```

### Dados sintéticos

O conjunto pronto está no Hugging Face, sem precisar gerar nada:

```python
from datasets import load_dataset

messages = load_dataset("G0m7s/deliverius-ai", "messages", revision="v1.0.0")
```

As configurações são `messages`, `messages_holdout`, `fraud_history`, `policy_qa` e `e2e_scenarios`. Para gerar de
novo, as chaves do Gemini e do Groq (cota gratuita) entram no `.env`. A geração completa faz cerca de 345 chamadas ao Gemini
e 55 ao Groq, dentro da cota de um dia; o cache de respostas permite retomar sem pagar de novo, e repetir a geração com
o mesmo cache produz arquivos idênticos byte a byte.

```bash
uv run python -m deliverius.cli datagen world --seed 42      # só o mundo simulado, sem LLM
uv run python -m deliverius.cli datagen scenarios --seed 42  # cenários de ponta a ponta, sem LLM
uv run python -m deliverius.cli datagen all --seed 42        # tudo, em data/generated/v<versão>/, com manifest.json
uv run python -m deliverius.cli datagen validate-human data/human-test/human_test.csv
```

O formato do conjunto de teste escrito por pessoas está em [`data/human-test/README.md`](data/human-test/README.md).

### Antifraude

```bash
uv run python -m research.fraud.gate                # baixa dataset e modelo nas revisões fixas e mede a porta
uv run python -m research.fraud.train               # treino, escolha, teste e registro no MLflow (mlruns/)
uv run python -m research.fraud.publish             # ensaio da publicação; --confirm envia
uv run python -m deliverius.cli fraud fetch-model   # o modelo que a aplicação carrega, conferido por SHA-256
```

### Pesquisa de intenção

Quatro abordagens comparadas pelo mesmo código, sobre o texto já mascarado, com a regra de escolha escrita antes dos
experimentos (`docs/ml-e-avaliacao.md`, seção 1.3). Na ordem:

```bash
uv run python -m research.intent.train                     # 1 e 2: TF-IDF e e5 + logística, só treino e validação
uv run python -m research.intent.llm_baseline              # 4: o LLM na amostra da validação (Postgres no ar; sai com 2 se a cota do dia acabar)
# 3: o notebook research/intent/notebooks/finetune_bertimbau.ipynb roda research.intent.finetune numa GPU do Kaggle
uv sync --group research --group finetune                  # torch e transformers, só para exportar
uv run python -m research.intent.export --checkpoint <pasta da execução>   # ONNX int8, conferido contra o PyTorch
uv run python -m research.intent.evaluate                  # a avaliação final, uma vez, nos três testes
uv run python -m research.intent.publish                   # ensaio da publicação do vencedor; --confirm envia
uv run pytest -m finetune --no-cov                         # ajuste fino e exportação de um BERT minúsculo
```

## Qualidade

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy                          # strict em src/
uv run lint-imports                  # fronteiras entre módulos
uv run pytest                        # unitários, cobertura mínima de 85%
uv run pytest -m integration --no-cov   # integração: o Testcontainers sobe um Postgres com pgvector
```

## Como o projeto é desenvolvido

- **Spec-driven development:** proposta, spec, design e tarefas no OpenSpec antes do código; o design de cada change
  responde a um checklist de robustez (corrida, idempotência, timeout, limites, falha no meio, eventos de log) em
  [`docs/robustez.md`](docs/robustez.md).
- **Monólito modular** com fronteiras verificadas pelo import-linter: regras puras (`policy`, `privacy`) não importam
  banco, web, log nem LLM. Detalhes em [`docs/arquitetura.md`](docs/arquitetura.md).
- **Segurança de LLM:** OWASP LLM Top 10 e riscos agênticos mapeados, regras contra prompt injection e checklist de
  revisão em [`docs/seguranca.md`](docs/seguranca.md); autorização com matriz de acesso em
  [`docs/autorizacao.md`](docs/autorizacao.md).
- **Avaliação:** ciclo de vida de ML com critério de escolha fixado antes dos experimentos e evals no estilo τ-bench em
  [`docs/ml-e-avaliacao.md`](docs/ml-e-avaliacao.md).

| Documento | Conteúdo |
| --- | --- |
| [`arquitetura.md`](docs/arquitetura.md) | Estilo, módulos, regras de import, agente, camada de LLM, RAG |
| [`fluxos-do-agente.md`](docs/fluxos-do-agente.md) | Os 7 cenários com o estado final esperado |
| [`modelo-dominio.md`](docs/modelo-dominio.md) | Dados, tabelas, política, gerador de dados sintéticos |
| [`ml-e-avaliacao.md`](docs/ml-e-avaliacao.md) | Ciclo de vida de ML, evals e CI |
| [`antifraude.md`](docs/antifraude.md) | Relatório do antifraude: escolha do modelo, teste, custo, calibração, SHAP e limites |
| [`seguranca.md`](docs/seguranca.md) | OWASP LLM, LGPD, mascaramento, provedores |
| [`observabilidade.md`](docs/observabilidade.md) | Traces, logs, métricas, alertas |
| [`decisoes-arquitetura.md`](docs/decisoes-arquitetura.md) | Todas as decisões, com alternativas |
| [`roadmap.md`](docs/roadmap.md) | Changes, dependências e critérios de pronto |

## Licença

Código sob a licença [MIT](LICENSE). Os dados sintéticos publicados no Hugging Face seguem a licença indicada no
*dataset card*.
