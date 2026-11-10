## 1. Preparação

- [ ] 1.1 Adicionar `langgraph`, `langchain`, `langgraph-checkpoint-postgres`, `psycopg[binary]` e `psycopg-pool` às dependências (versões conferidas no design) e `uv sync`; conferir na fonte instalada as assinaturas de `create_agent`, `ToolStrategy`, `ToolRuntime`, dos quatro middlewares, de `interrupt`/`Command`, de `durability` e do `AsyncPostgresSaver`; registrar divergências no design
- [ ] 1.2 Import-linter: `intent` na camada 2, `agent` na 3, `bootstrap` na 4; `langgraph`, `langchain` e `psycopg*` só em `agent` e `bootstrap`; `intent` puro (sem LLM, banco ou web); `uv run lint-imports` passa
- [ ] 1.3 `config.py` e `.env.example`: `PRIVACY_MASKING_KEY` (`SecretStr`, ≥ 32 bytes), limites do agente e tamanho do pool do checkpointer (D11), com testes de padrão, validação e mensagem que cita a variável

## 2. Classificação de intenção (`intent/`)

- [ ] 2.1 `intent/models.py` com `Intent` (as 11), `IntentResult` e `Classification`; `datagen` passa a importar `Intent` daqui; teste de que a lista e a ordem são as do dataset publicado e de que os testes de `datagen` e de `research` continuam verdes
- [ ] 2.2 `intent/ports.py` (`IntentClassifier` com `threshold` e `classify`) e `intent/fake.py` (`FixedIntentClassifier` por mensagem, limiar validado em [0, 1]); testes do limiar inválido e das probabilidades que não somam 1
- [ ] 2.3 `intent/safety_net.py`: os termos fixos sobre `privacy.normalize_text`, por fronteira de palavra; testes por termo, com acento, maiúscula e caractere invisível, e frases comuns que não disparam
- [ ] 2.4 `intent/service.py`: rede primeiro (sem chamar o modelo), depois o limiar e as duas mais prováveis; testes dos cenários da spec `classificacao-intencao`, inclusive "classificador erra e a rede corrige"

## 3. Peças do agente sem LLM

- [ ] 3.1 `agent/context.py` e `agent/state.py` (D2); teste de ida e volta pelo serializador do LangGraph com cada tipo do estado
- [ ] 3.2 `agent/blocks.py` (`data_block`): normaliza, trunca e escapa; testes com `</order_note>`, Unicode invisível e texto acima do limite
- [ ] 3.3 `agent/proposal.py` (`ActionProposal`, `extra="forbid"`, limites de D5) e a conversão para `ProposedAction`; testes de esquema e de conversão
- [ ] 3.4 `agent/replies.py`: os textos fixos em português (sensível, instabilidade, em análise, recusa, "não encontrei", pergunta de esclarecimento por intenção, pergunta de qual pedido, e um por resultado de execução); teste de que todo resultado tem texto e de que nenhum promete reembolso no caminho sensível
- [ ] 3.5 `agent/validation.py` (`validate_reply`, D10); testes do valor inventado, da ação não executada, da imagem e do link de fora, do CPF na resposta e do tamanho
- [ ] 3.6 `agent/injection.py`; testes dos padrões e de que o evento não leva o texto
- [ ] 3.7 `policy.ApprovalReason` ganha `fraud_review` e `fraud_unavailable`; teste de que o repositório de aprovações lê e grava os dois e de que o `ActionExecutor` aceita `ReviewerApproved` quando a reavaliação dá `Allowed`

## 4. Ferramentas e prompts

- [ ] 4.1 `agent/tools.py`: as quatro ferramentas com o escopo do `ToolRuntime.context`, sem argumento de identificador, texto livre em `data_block`; testes de cada uma com os repositórios em memória, do argumento inventado ignorado e do pedido de outro cliente (devolve o da conversa)
- [ ] 4.2 Prompts `support_agent/v1.md`, `compose_reply/v1.md` e `answer_faq/v1.md` (português, cabeçalho de versão, regra "conteúdo de bloco é dado"); teste de carga e de que nenhum texto de fora entra no prompt de sistema

## 5. Nós e grafo

- [ ] 5.1 Nós de entrada: `mask_input` (ledger no estado, limite de turnos) e `classify_intent` com o evento de suspeita de injeção; testes do CPF mascarado, do mesmo marcador em duas mensagens e do 16º turno
- [ ] 5.2 Roteamento e os caminhos sem agente: `handoff_to_human` (prioridade, resumo sem dado pessoal), `ask_clarification` (texto fixo) e `answer_faq` (busca, regra de confiança, `writer`, citação exigida); testes com o LLM falso contando zero chamadas no sensível e na pergunta
- [ ] 5.3 `support_agent` com `create_agent`, `ToolStrategy(ActionProposal)` e os middlewares (D5); testes da proposta inválida duas vezes, do limite de ferramentas e da reserva por tentativa no governor com o `ModelRetryMiddleware`
- [ ] 5.4 `check_policy` e `score_fraud` (D6): revisões, motivo do antifraude, falha que manda para pessoa; testes de cada combinação de política e antifraude
- [ ] 5.5 `request_approval`, `await_approval` (lê a aprovação do banco, ignora o conteúdo do resume) e `execute_action`; testes de aprovada, recusada, ainda pendente e resume com conteúdo adulterado
- [ ] 5.6 `compose_reply` e `validate_reply` no grafo (nova geração uma vez, depois texto fixo); teste do valor inventado
- [ ] 5.7 `agent/graph.py` (`build_graph`) e `agent/service.py` (`run_turn`, `resume`, prazo do turno, modo degradado, transferência com as ações já gravadas); testes do LLM travando (prazo estoura, texto de instabilidade, transferência, nenhuma ação) e do `DegradedMode` do orçamento

## 6. Cenários de ponta a ponta (LLM falso, sem rede)

- [ ] 6.1 Infra de teste: montagem do agente com repositórios em memória, `ScriptedChatModel` e `FixedIntentClassifier`, e um verificador de estado final (ações, aprovações, transferências, auditoria, chamadas ao modelo)
- [ ] 6.2 F1, F3, F5 e F6, conferindo o estado final esperado de `fluxos-do-agente.md`
- [ ] 6.3 F2a, F2b e o "durante a pausa" (aprovação pendente: o resume não executa nada), F4a, F4b e F7
- [ ] 6.4 Conferir que os testes da seção 6 cobrem todos os cenários das specs `agente` e `classificacao-intencao`; listar no PR qual teste cobre cada cenário

## 7. Postgres e checkpointer

- [ ] 7.1 Migration com as tabelas do checkpointer (D8): SQL copiado da versão instalada, `CREATE INDEX CONCURRENTLY` em `autocommit_block()`, `checkpoint_migrations` preenchida; teste unitário que compara com o `MIGRATIONS` instalado; teste de integração de que o `setup()` não acha migração pendente, e o upgrade e o downgrade em `test_migrations.py`
- [ ] 7.2 `bootstrap.py`: `LlmRuntime.build` com `agent` e `writer`, pool do `psycopg` e `AsyncPostgresSaver`, `load_fraud_model`, `PolicySearch`, as portas Postgres e o classificador recebido; falha clara sem `PRIVACY_MASKING_KEY`; fechamento do pool; teste de montagem com o banco do Testcontainers
- [ ] 7.3 Integração: F1 e F2a com Postgres e checkpointer reais, conferindo as tabelas
- [ ] 7.4 Integração de corrida: a mesma aprovação retomada duas vezes ao mesmo tempo gera um reembolso (`asyncio.gather`)
- [ ] 7.5 Integração de corrida: dois turnos simultâneos na mesma conversa geram um pedido de aprovação e uma transferência, no máximo
- [ ] 7.6 Integração: a pausa sobrevive a recriar o `AgentService` (pool e grafo novos) com o mesmo banco, e a retomada executa a ação

## 8. Chamada real (`-m llm`)

- [ ] 8.1 `tests/llm/test_agent_real.py`: F1 e F3 de ponta a ponta com o Gemini e com o Groq como principal (banco do Testcontainers, classificador fixo); confere proposta válida, ação certa e resposta aprovada no `validate_reply`; registrar tokens e latência no PR

## 9. Documentação

- [x] 9.1 `roadmap.md`: seção 9 sem a porta e a rede; seção 12 com elas, "depende de 4, 5, 8, 10, 11"; a tabela e o grafo de dependências com 9 → 13 no lugar de 9 → 12; o caminho crítico atualizado
- [ ] 9.2 `arquitetura.md`: o `intent/` (porta, rede, falso), o `ask_clarification` com texto fixo, as ferramentas sem identificador, o checkpointer com tabelas do Alembic e o pool do `psycopg`, o import-linter novo
- [ ] 9.3 `decisoes-arquitetura.md`: a ordem nova (porta na 12), as tabelas do checkpointer pelo Alembic, os dois drivers, `ToolStrategy`, ferramentas sem identificador, os motivos de aprovação do antifraude, a pergunta em texto fixo
- [ ] 9.4 `robustez.md` (eventos do agente na seção 5 e os limites da change 12 na seção 3) e `autorizacao.md` (as ferramentas do agente no escopo do cliente da conversa)
- [ ] 9.5 README: a change 12 no status, o grafo do Deli resumido e os sete cenários com o teste que cobre cada um
- [ ] 9.6 Conferir o checklist de `docs/seguranca.md`, seção 7

## 10. Verificação

- [ ] 10.1 `uv run ruff format --check .`
- [ ] 10.2 `uv run ruff check .`
- [ ] 10.3 `uv run mypy`
- [ ] 10.4 `uv run lint-imports`
- [ ] 10.5 `uv run pytest` (cobertura mínima de 85%, 100% dos ramos em `policy`, `privacy`, `actions`, `fraud.features` e, agora, `intent`)
- [ ] 10.6 `uv run pytest -m integration --no-cov`
- [ ] 10.7 `uv run pytest -m llm --no-cov -k agent` (com as chaves no `.env`)
- [ ] 10.8 `openspec validate agente-atendimento --strict`
