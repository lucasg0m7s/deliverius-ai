# Requisitos do projeto

As 26 capacidades que o Deliverius AI se compromete a demonstrar, cada uma ligada à parte do projeto que a mostra,
à evidência pública que qualquer pessoa consegue abrir e às changes do OpenSpec que a entregam (`roadmap.md`).
Toda proposal cita os IDs que cobre.

| ID | Requisito | Onde o projeto demonstra | Evidência pública | Changes |
| --- | --- | --- | --- | --- |
| R01 | Engenharia de software sólida | Módulos com dependências explícitas e verificadas, mypy estrito, testes, CI | Repositório, badge do CI | `fundacao-projeto`, `nucleo-dominio` e todas as seguintes |
| R02 | Software para produtos de IA | API de atendimento com conversa em streaming, aprovações e métricas | `/docs` da API, demo | `api-atendimento`, `demo-gradio` |
| R03 | Modelos em operação, com ênfase em LLMs | Camada de LLM com reserva, custo por chamada e orçamento; modelos próprios servidos pela API com revisão fixa | `llm/`, `intent/`, `fraud/` | `camada-llm`, `classificador-intencao`, `antifraude-reembolso` |
| R04 | Treino e ajuste fino de modelos | BERTimbau ajustado para intenção; antifraude (LightGBM contra regressão logística, que venceu); LoRA num decodificador pequeno | Model cards no Hugging Face | `pesquisa-intencao`, `antifraude-reembolso`, `lora-intencao` |
| R05 | Pipelines de IA reproduzíveis | Geração de dados, treino, registro (MLflow) e avaliação, cada etapa por comando | `research/`, CI | `gerador-dados`, `pesquisa-intencao`, `antifraude-reembolso`, `evals-agente` |
| R06 | Implementações de ML abertas | Baselines, validação, análise de erros, calibração, SHAP | `research/` | `pesquisa-intencao`, `antifraude-reembolso` |
| R07 | Modelos e datasets publicados | Classificador de intenção, antifraude, datasets sintéticos e, como extra, o modelo LoRA | Links no README | `gerador-dados`, `pesquisa-intencao`, `antifraude-reembolso`, `lora-intencao` |
| R08 | Pesquisa com hipótese e método | "Formas de classificar intenção": qualidade × latência × custo; comparação de variantes da busca | `docs/pesquisa-intencao.md` | `pesquisa-intencao`, `busca-politicas`, `lora-intencao` |
| R09 | Notebook público reproduzível | Treino no Kaggle; dataset e notebook do antifraude | Links no README | `pesquisa-intencao`, trilha de publicação |
| R10 | Artigo técnico | O desenho do agente e os resultados medidos | Link no README | Trilha de publicação |
| R11 | Desenvolvimento assistido por IA | Fluxo spec-driven com OpenSpec e Claude Code, versionado | `openspec/`, histórico | `fundacao-projeto` e todas as seguintes |
| R12 | Agente que age | Agente LangGraph que resolve (reembolso, cupom, cancelamento) com aprovação humana | `agent/`, demo | `acoes-e-aprovacoes`, `agente-atendimento` |
| R13 | Vários provedores de LLM | Gemini, compatível com OpenAI (Groq, Ollama) e Anthropic atrás do mesmo contrato | Tabela de evals por provedor | `camada-llm`, `evals-agente` |
| R14 | Engenharia de prompt | Prompts versionados, saída estruturada, regressão de prompt pelos evals | `agent/prompts/` | `agente-atendimento`, `evals-agente` |
| R15 | Automação com ganho medido | Taxa de automação medida; fluxo n8n de escalonamento e relatório diário | Relatórios, painel, `automations/` | `evals-agente`, `metricas-operacao`, `automacao-n8n` |
| R16 | Painel e análise de dados | Painel de operação: automação, custo, latência, intenções, fraude | Aba "Painel" da demo | `metricas-operacao`, `demo-gradio` |
| R17 | Demo pública e usável | Gradio no Hugging Face Spaces, banco na Neon, sandbox por visitante | Link da demo | `sandbox-demo`, `demo-gradio`, `deploy-demo` |
| R18 | Python e ecossistema de ML | Projeto em Python 3.13 com uv, ruff, mypy, pytest | Repositório | `fundacao-projeto` |
| R19 | RAG e busca vetorial | Busca híbrida nas políticas: pgvector + full-text em português + RRF, com citação | `retrieval/`, recall@3 | `busca-politicas` |
| R20 | Avaliação de LLMs | Clientes simulados, estado final do banco, pass^k, juiz calibrado (kappa) | `evals/`, `docs/resultados/` | `evals-agente`, `juiz-calibrado` |
| R21 | Observabilidade, custo e latência | OpenTelemetry + Langfuse; p50/p95 e custo por conversa | Traces, relatórios, painel | `camada-llm`, `observabilidade` |
| R22 | Segurança de LLM | Mascaramento, política em código, prompt injection direto e indireto, autorização na API | Evals de segurança | `motor-politica`, `mascaramento-dados`, `api-atendimento`, `evals-agente` |
| R23 | Docker, CI/CD e nuvem | docker compose, Dockerfile, GitHub Actions, deploy por tag | Workflows | `fundacao-projeto`, `deploy-demo` |
| R24 | Fundamentos de ML clássico | TF-IDF, regressão logística, LightGBM, split por tempo, PR-AUC, limiar por custo | `research/` | `pesquisa-intencao`, `antifraude-reembolso` |
| R25 | Comunicação de resultados | README com problema, números e trade-offs; ADRs; artigo | README, `docs/` | `fechamento-nucleo`, trilha de publicação |
| R26 | MCP e tool calling | Servidor MCP com as ferramentas de leitura do atendimento | `mcp/` | `servidor-mcp` |

Fora do escopo, de propósito: treinar um LLM do zero, GPU própria e Kubernetes.
