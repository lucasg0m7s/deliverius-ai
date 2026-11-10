## Context

O desenho do agente já está decidido em `docs/arquitetura.md`, seção "Desenho do agente" (grafo, nós, ferramentas,
estado, limites e alternativas), e não é rediscutido aqui. Este design fixa o que falta para codificar: a forma do
estado, como cada módulo existente entra, onde ficam o escopo e o prazo, e como o checkpointer convive com o
Alembic.

O que já existe e é usado como está:

- `privacy.mask_text(text, ledger, key)` e `MarkerLedger`; `privacy.find_personal_data` e `normalize_text`;
- `policy.evaluate` e `policy.proposal_key`;
- `fraud.claim_context`, `FraudScorer`, `PostgresFraudContextReader`, `load_fraud_model`, `FixedFraudScorer`;
- `approvals.ApprovalService` (pedido idempotente, que muda a conversa para `awaiting_approval`; decidir a devolve
  para `open`) e `HandoffService` (que muda a conversa para `transferred`);
- `actions.ActionExecutor` (trava do cliente e do pedido, política reavaliada, chave única);
- `retrieval.PolicySearch` (com a regra de confiança);
- `llm.LlmRuntime`, `RoleModels`, `call_or_degrade`, `call_structured`, `load_prompt`, `call_metadata` e o
  `ScriptedChatModel`, que já roteiriza ferramentas, saída estruturada, falha e travamento;
- a tabela `conversations` (sandbox, cliente, pedido anulável, status), criada na change 11.

Versões conferidas no PyPI em 2026-11-10: `langgraph` 1.2.20, `langchain` 1.4.9 (exige `langchain-core` ≥ 1.7.5; o
projeto tem 1.7.6), `langgraph-checkpoint-postgres` 3.2.0, `psycopg` 3.3.8, `psycopg-pool` 3.3.3. No código-fonte da
3.2.0: as tabelas do checkpointer vêm de uma lista `MIGRATIONS` de SQL, aplicada por `setup()` e registrada em
`checkpoint_migrations`, e três índices usam `CREATE INDEX CONCURRENTLY`. O `create_agent` aceita `middleware`,
`context_schema` e `response_format`; existem `ModelFallbackMiddleware`, `ModelRetryMiddleware`,
`ModelCallLimitMiddleware`, `ToolCallLimitMiddleware`, `ToolStrategy` e `ToolRuntime`.

## Goals / Non-Goals

**Goals:**

- O grafo inteiro com LLM falso, sem rede, cobrindo os sete cenários de `fluxos-do-agente.md`.
- Um ponto de entrada pequeno para a change 13: `AgentService.run_turn(...)` e `AgentService.resume(...)`.
- A porta `IntentClassifier` e a rede de palavras-chave, para o agente não depender do vencedor da pesquisa.
- O checkpointer no mesmo Postgres, com as tabelas criadas pelo Alembic.

**Non-Goals:**

- Rotas, lease, `client_message_id`, tabela de mensagens: change 13. A change 12 confia que a 13 chama um turno por
  vez por conversa.
- Adaptador real do classificador: change 9. Até lá, só o classificador falso existe, e o `bootstrap` recebe o
  classificador como argumento.
- Traces: change 14.

## Decisions

### D1. Estrutura

```
src/deliverius/
├── intent/
│   ├── models.py          Intent (as 11, saindo de datagen), IntentResult (rótulo, probabilidades, confiança,
│   │                      by_safety_net), Classification (resultado final com is_uncertain e as 2 mais prováveis)
│   ├── ports.py           IntentClassifier: threshold e async classify(masked_text) -> IntentResult
│   ├── safety_net.py      Os termos fixos e matches_sensitive(text) sobre privacy.normalize_text
│   ├── service.py         classify(classifier, masked_text): rede primeiro, depois o modelo e o limiar
│   └── fake.py            FixedIntentClassifier: resposta por mensagem, com padrão; valida o limiar
├── agent/
│   ├── state.py           TurnState (TypedDict) e os modelos que vivem nele
│   ├── context.py         AgentContext (frozen): scope, conversation_id, order_id; nunca no estado
│   ├── proposal.py        ActionProposal (o esquema que o LLM preenche) e a conversão para ProposedAction
│   ├── tools.py           As 4 ferramentas de leitura, com o escopo do ToolRuntime.context
│   ├── blocks.py          data_block(label, text, max_chars): o único lugar que põe texto de fora no prompt
│   ├── prompts/           support_agent/v1.md, compose_reply/v1.md, answer_faq/v1.md
│   ├── replies.py         Os textos fixos ao cliente (português) e a escolha pelo resultado do turno
│   ├── validation.py      validate_reply: valores, ações, links, imagens e dado pessoal
│   ├── injection.py       Os padrões de suspeita de injeção (só o evento de log)
│   ├── nodes/             Um arquivo por grupo de nós (entrada, roteamento, agente, política e fraude,
│   │                      aprovação, execução, resposta, transferência)
│   ├── graph.py           build_graph(deps) -> o StateGraph compilado
│   ├── deps.py            AgentDeps: as portas e serviços que os nós usam (dataclass, montado no bootstrap)
│   └── service.py         AgentService: run_turn e resume, com o prazo do turno e o modo degradado
└── bootstrap.py           build_agent(settings, ..., intent_classifier) -> AgentService; o checkpointer e o pool
```

O `datagen` passa a importar `Intent` de `intent/` (camada 5 importa a 2). Os valores não mudam, então o dataset
publicado e a pesquisa não mudam. Alternativa: deixar uma cópia no `agent/` → duas fontes da mesma regra, recusada.

### D2. Estado do turno e contexto

- **O estado** (`TurnState`, `TypedDict`, como em `arquitetura.md`) guarda só o que precisa sobreviver à pausa:
  as mensagens mascaradas da conversa, o `MarkerLedger`, a classificação, a proposta, a decisão da política, a nota
  do antifraude, o `approval_request_id`, o resultado da execução, a resposta, os contadores (revisões, turnos) e o
  `claimed_at` do turno. Os modelos dentro dele são dataclasses e enums do domínio, que o serializador do LangGraph
  guarda; um teste faz ida e volta pelo serializador com cada tipo.
- **O contexto** (`AgentContext`: `CustomerScope`, `conversation_id`, `order_id | None`) vem de quem chama, a cada
  execução, pelo `context=` do LangGraph, e **não** fica no estado. Assim, nem o LLM nem um estado adulterado
  escolhem o cliente ou o pedido. As ferramentas leem o contexto pelo `ToolRuntime`; os nós, pelo `Runtime`.
- **Compatibilidade:** campos novos no estado entram como opcionais (regra aditiva de `robustez.md`, seção 7). Os
  códigos de motivo usados no estado já são enums "só acrescentar".

### D3. Classificação: rede primeiro, depois o modelo

`intent.service.classify` roda a rede de palavras-chave sobre `normalize_text` do texto mascarado; achando um
termo, devolve `sensitive` com `by_safety_net=True` **sem chamar o classificador** (menos trabalho e o mesmo
resultado). Senão, chama o classificador, confere que as probabilidades cobrem as 11 intenções e somam 1 (±1e-6) e
aplica o limiar. Os termos são radicais curtos e fixos (`alerg`, `passando mal`, `passei mal`, `vomit`, `intoxic`,
`hospital`, `acidente`, `agred`, `agress`, `assedi`, `ameac`), casados por fronteira de palavra no texto sem acento.
Lista fixa no código, sem configuração: mudar a lista muda o comportamento de segurança e passa por PR com teste.

Alternativa: a rede só depois do classificador, para "corrigir" → igual em resultado, mas chama o modelo à toa.

### D4. Ferramentas de leitura

- `get_order`, `get_delivery_timeline` e `get_customer_summary` **não recebem identificador** do LLM: o esquema da
  ferramenta não tem o argumento, e elas leem o `order_id` e o escopo do contexto. Assim o cenário "LLM pede o
  pedido de outro cliente" se resolve por construção, e o teste confere que um argumento extra inventado é ignorado.
  Alternativa: aceitar o `order_id` e conferir contra o contexto → mais código e uma mensagem de erro que ensina o
  LLM a tentar de novo.
- `get_customer_summary` reaproveita o `FraudContextReader.history` (90 dias, já com índice e limite de linhas) e
  devolve contagens e valores, sem texto livre.
- `search_policy(query)` recebe a consulta do LLM (texto, limitado a `MAX_QUERY_CHARS`), devolve os trechos com o
  identificador do documento e da seção, cada um num `data_block`.
- **Saída como dado:** todo texto livre (observação do pedido, nome de item, trecho de política) passa por
  `blocks.data_block`, que normaliza, trunca (observação: 500 caracteres; trecho: 1.500) e escapa `<` e `>` dentro
  do bloco, para `</order_note>` nunca fechar o bloco. Um teste com textos maliciosos cobre a função.

### D5. O `support_agent`

- `create_agent(model=primary, tools=[...4], system_prompt=<support_agent v1>, context_schema=AgentContext,
  response_format=ToolStrategy(ActionProposal), middleware=[...])`, compilado uma vez e usado como subgrafo.
- **Middlewares:** `ModelFallbackMiddleware(backup)` com os dois modelos de `RoleModels`; `ModelRetryMiddleware`
  (2 novas tentativas, espera exponencial com jitter, só para os erros repetíveis, a mesma lista de `llm`);
  `ToolCallLimitMiddleware(run_limit=8)` e `ModelCallLimitMiddleware(run_limit=10)`, com saída por erro. O
  `ModelRetryMiddleware` chama o mesmo objeto de modelo, então cada tentativa passa pelo callback do governor; um
  teste conta uma reserva por tentativa.
- **`ToolStrategy`** em vez do JSON Schema nativo: funciona igual no Gemini, no Groq e no `ScriptedChatModel`, e o
  erro de validação volta ao modelo como mensagem de ferramenta (a "nova tentativa com o erro" de `arquitetura.md`).
  Depois de 2 propostas inválidas, o nó transfere.
- **`ActionProposal`** (Pydantic, `extra="forbid"`): `outcome` (`act`, `no_action`, `need_human`), e, em `act`,
  `kind`, `reason` (`ClaimReason`), `amount_cents` (inteiro positivo, até R$ 10.000), `items`
  (`order_item_id`, `quantity`, no máximo 20) e `policy_document` (o identificador citado); um `note` curto
  (≤ 300 caracteres), que só vai para o resumo da transferência depois de passar por `find_personal_data`. A
  conversão para `ProposedAction` é código; item que não é do pedido é recusado pela política, não aqui.
- O prazo do agente (45 s de `ROLE_CONFIGS`) é limitado também pelo que resta do prazo do turno.

### D6. Política, antifraude e o motivo da aprovação

- `check_policy` chama `policy.evaluate` com o pedido e o histórico lidos sem trava (é uma prévia; a decisão que
  vale é a reavaliação dentro da trava do `ActionExecutor`). `Denied` com revisões < 2 volta ao `support_agent`
  com o motivo e o teto num `data_block`; na terceira negativa, transfere.
- `score_fraud` monta `claim_context` e pontua. Resultado:
  - `Allowed` e o antifraude não pede revisão → `execute_action` com `Automatic()`;
  - `Allowed` e pede revisão → `NeedsApproval(teto, documento, (FRAUD_REVIEW,))`;
  - `NeedsApproval` → os motivos da política, mais `FRAUD_REVIEW` se o antifraude pedir;
  - `FraudScoringError` (ou qualquer erro do leitor do histórico) → motivo `FRAUD_UNAVAILABLE`, sem nota.
- **Dois valores novos em `policy.ApprovalReason`** (`fraud_review`, `fraud_unavailable`): o enum é "só
  acrescentar", a coluna `policy_reasons` é `text[]` sem `CHECK`, e o `ActionExecutor` aceita `ReviewerApproved`
  quando a reavaliação dá `Allowed`. Alternativa: um campo próprio de motivo do antifraude na aprovação → muda a
  tabela, o modelo e o console para dizer a mesma coisa.

### D7. Aprovação, pausa e retomada

- `request_approval` chama `ApprovalService.request` (idempotente) e guarda o `approval_request_id` no estado; a
  resposta "em análise" é texto fixo com o prazo.
- `await_approval` só chama `interrupt({"approval_request_id": ...})`. Na retomada (`Command(resume=...)`), o nó
  **ignora o conteúdo do resume** e lê a aprovação por `ApprovalService.get` com o escopo do contexto: `approved` →
  `execute_action` com `ReviewerApproved(id, decided_by)`; `rejected` → `compose_reply` com a recusa; `pending` →
  `interrupt` de novo. Quem decide é o banco, não o chamador.
- **Retomada dupla:** as duas execuções chegam ao `ActionExecutor`, que trava cliente e pedido e usa a chave única
  da proposta; a segunda devolve `Executed(replayed=True)`. Um teste de integração dispara as duas juntas.
- O `AgentService.resume` só é chamado pela change 13 depois de a decisão estar gravada.

### D8. Checkpointer e Alembic

- `AsyncPostgresSaver` sobre um `psycopg_pool.AsyncConnectionPool` próprio (mín. 1, máx. 4, `timeout` de 5 s,
  `autocommit=True`, `prepare_threshold=0`, `row_factory=dict_row`, como a documentação pede), aberto no
  `bootstrap` e fechado no desligamento. As opções `statement_timeout` e `lock_timeout` vão no `options` da conexão,
  como no `db.engine`. Durabilidade `sync` em toda execução.
- **As tabelas são do Alembic:** a migration nova copia, literalmente, as instruções de `MIGRATIONS` da versão
  instalada e grava os números em `checkpoint_migrations`, com os três `CREATE INDEX CONCURRENTLY` dentro de
  `autocommit_block()`. A aplicação nunca chama `setup()`. Um teste de integração confere que, depois do
  `alembic upgrade head`, o `setup()` não acha migração pendente; e um teste unitário compara o texto copiado com o
  `MIGRATIONS` instalado, para uma atualização da biblioteca exigir uma migration nova. Alternativas: `setup()` na
  subida → DDL fora do Alembic e com corrida entre instâncias; escrever um checkpointer sobre o `asyncpg` → um
  componente crítico mantido à mão.
- **Dois drivers** (`asyncpg` para a aplicação e `psycopg` para o checkpointer) é o custo aceito; o `psycopg` só é
  importado por `bootstrap` e `agent` (contrato do import-linter).

### D9. Prazo do turno e modo degradado

- `AgentService.run_turn` envolve a execução do grafo em `asyncio.timeout(turn_deadline)` (60 s). No estouro, ou
  quando um nó devolve `DegradedMode`, o serviço lê o último checkpoint (`aget_state`) para saber se houve execução,
  cria a transferência com `HandoffService` (resumo com as ações já gravadas) e devolve o texto fixo de
  instabilidade. Nenhuma ação nova acontece fora do grafo.
- Cancelamento no meio de `execute_action` desfaz a transação inteira (`robustez.md`, seção 4); a ação ou foi
  gravada inteira ou não foi. `CancelledError` nunca é engolido.
- Os prazos de cada chamada de LLM recebem `min(prazo do papel, prazo restante do turno)`.
- **Limite de turnos:** o contador está no estado; no 16º turno, o grafo vai direto para `handoff_to_human`, antes
  de classificar.

### D10. Resposta e validação

- `compose_reply` usa o papel `writer` com o prompt `compose_reply/v1`, recebendo **só os fatos** (resultado,
  decisão, motivo, documento citado, valores formatados) em blocos de dado; o texto do cliente vai como dado também.
- `validate_reply`: (1) todo `R$ x` da resposta está no conjunto de valores dos fatos; (2) toda ação afirmada
  ("reembolsei", "cupom", "cancelei") corresponde a uma ação executada; (3) nenhuma imagem em markdown e nenhum link
  fora da lista permitida (vazia por padrão); (4) `find_personal_data(normalize_text(resposta))` vazio; (5) até
  2.000 caracteres. Reprovada → uma nova geração com o motivo; reprovada de novo → texto fixo do resultado. As
  regex rodam sobre texto já limitado em tamanho.
- `ask_clarification` usa **texto fixo** com as duas intenções mais prováveis traduzidas para o cliente, sem LLM
  (o `arquitetura.md` previa o `writer`): é mais barato, determinístico e não manda ao LLM uma mensagem que nem o
  classificador entendeu. Registrado em `decisoes-arquitetura.md`.
- `answer_faq`: `PolicySearch.search`; resultado não confiável → texto fixo "não encontrei" com a oferta de uma
  pessoa; confiável → `writer` com `answer_faq/v1` e os trechos em blocos; a validação exige ao menos uma citação de
  documento que estava nos trechos.

### D11. Chave do mascaramento e configuração

`PRIVACY_MASKING_KEY` (obrigatória, `SecretStr`, ≥ `privacy.MIN_KEY_BYTES`) é lida pelo `config.py`; a montagem do
agente falha citando a variável se faltar. Também entram: `AGENT_TURN_DEADLINE_SECONDS` (60),
`AGENT_MAX_TOOL_CALLS` (8), `AGENT_MAX_REVISIONS` (2), `AGENT_MAX_TURNS` (15), `AGENT_CHECKPOINT_POOL_SIZE` (4). A
API (`api/main.py`) ainda não monta o agente (change 13), então a subida da API não exige a chave nesta change.

### D12. Avaliação desta change

- **Testes de ponta a ponta** com `ScriptedChatModel` e `FixedIntentClassifier`, um por cenário de
  `fluxos-do-agente.md` (F1 a F7, com F2a, F2b, F4a e F4b), conferindo o **estado final do banco** (ações,
  aprovações, transferências, auditoria) e as chamadas feitas ao modelo, no estilo do τ-bench. Rodam em memória no
  `pytest` padrão e uma segunda vez contra o Postgres (com o checkpointer real) nos de integração.
- **Chamada real** (`-m llm`): F1 e F3 de ponta a ponta com o Gemini e com o Groq como principal. Conferido: a
  proposta valida, a ação certa existe, a resposta passa no `validate_reply`. Taxa de acerto e custo ficam para os
  evals (change 15).
- **Métricas que esta change já registra** (para a 15 agregar): intenção e confiança, caminho, revisões, chamadas
  de ferramenta, validação reprovada, modo degradado.

### D13. Eventos de log

`agent.turn_started`, `agent.turn_finished` (caminho, duração, ações), `agent.intent_classified` (intenção,
confiança, `by_safety_net`), `agent.routed`, `agent.proposal_invalid`, `agent.policy_decided`,
`agent.fraud_scored`, `agent.fraud_unavailable`, `agent.paused`, `agent.resumed`, `agent.action_executed`,
`agent.reply_rejected` (motivo), `agent.degraded` (motivo), `agent.limit_reached` (qual),
`agent.handoff_created`, `security.injection_suspected`. Nenhum leva texto do cliente ou do LLM. Viram métrica no
painel (E1): taxa de transferência, de modo degradado, de resposta reprovada e de suspeita de injeção.

## Checklist de `robustez.md`, seção 11

- **Autorização:** nenhuma rota nova (change 13). As quatro ferramentas são novas e leem com o `CustomerScope` do
  contexto; `autorizacao.md` ganha a linha "ferramentas do agente: só o cliente da conversa", e o teste da matriz
  ganha as ferramentas quando as rotas existirem (13). Aqui, o teste do outro cliente cobre o escopo.
- **Corrida:** retomada dupla da mesma aprovação → trava e chave única no `ActionExecutor` (teste de integração com
  as duas juntas). Dois turnos na mesma conversa → responsabilidade da lease da change 13; aqui, um teste mostra que
  o pedido de aprovação e a transferência continuam únicos quando dois turnos correm juntos (as restrições da 11).
- **Idempotência:** nós reexecutados na retomada: `request_approval` (chave da conversa e da proposta),
  `execute_action` (chave da proposta), `handoff_to_human` (uma aberta por conversa). `await_approval` não tem
  efeito.
- **Timeout:** LLM pelos prazos do papel, limitados pelo prazo do turno; banco pelo `statement_timeout`; pool do
  checkpointer com `timeout` de 5 s; turno com 60 s.
- **Limite:** ferramentas 8, chamadas de modelo 10, revisões 2, turnos 15, mensagens do estado (as 20 últimas vão ao
  LLM), texto livre truncado nos blocos, resposta até 2.000 caracteres, pool do checkpointer 4.
- **Falha no meio:** checkpoint `sync` a cada passo; ação numa transação só; queda depois do `execute_action` e
  antes da resposta → na próxima execução a chave devolve a mesma ação.
- **Transação com rede ou LLM:** nenhuma. Os nós chamam serviços que abrem e fecham transações curtas.
- **Eventos de log:** D13.
- **Texto de fora:** a mensagem passa por `mask_text` (que normaliza); o texto do LLM passa pelo `validate_reply`;
  as regex novas (rede de segurança, injeção, valores) rodam sobre texto limitado (a mensagem tem até 1.000
  caracteres na API e `MAX_TEXT_LENGTH` no mascaramento) e não têm repetição aninhada.
- **Estado, banco e API:** estado novo (nada pausado existe ainda); banco aditivo (tabelas do checkpointer); API
  não muda.
- **Consultas:** nenhuma consulta nova da aplicação; as do checkpointer são da biblioteca, com os índices por
  `thread_id` que ela cria. As ferramentas usam as leituras existentes, com índice e limite.
- **Travas:** as do `ActionExecutor` (cliente e depois pedido, com `lock_timeout`). A migration cria tabelas vazias e
  índices `CONCURRENTLY`.
- **Configuração:** `PRIVACY_MASKING_KEY` obrigatória na montagem do agente, com mensagem que cita a variável; os
  limites têm padrão e validação (`gt=0`).

## Risks / Trade-offs

- [O Gemini às vezes não chama a ferramenta ou devolve proposta fora do esquema] → `ToolStrategy` com o erro de
  volta, uma nova tentativa e transferência; os evals (15) medem a taxa.
- [O serializador do LangGraph não aceitar um tipo do domínio no estado] → teste de ida e volta por tipo; o que
  falhar vira um modelo Pydantic na fronteira do estado.
- [A lista de termos sensíveis gera falso positivo ("acidente de percurso")] → o erro vai para o lado seguro (uma
  pessoa atende); a taxa entra no eval da 15.
- [Até a change 9, não existe classificador real] → o agente não sobe em produção antes da 9; a ordem nova do
  roadmap põe a 9 antes da 13.
- [Uma atualização do `langgraph-checkpoint-postgres` acrescentar migração] → o teste que compara o SQL falha e
  exige a migration nova.
- [Dois drivers de Postgres] → custo de dependência registrado; o pool do checkpointer é pequeno e separado.

## Migration Plan

`alembic upgrade head` cria as tabelas do checkpointer (aditivo; o rollback as remove, não há dados de produção).
Nenhum código em produção muda de comportamento: o agente só passa a ser chamado na change 13.
