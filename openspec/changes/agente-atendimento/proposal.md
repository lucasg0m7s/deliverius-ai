## Why

Change 12 do `docs/roadmap.md` (`agente-atendimento`, fase F3), que cobre os requisitos **R12** (ação com efeito
dentro da política e com aprovação humana), **R13** (LLM com ferramentas, saída estruturada e resiliência) e **R14**
(agente com fluxos testáveis de ponta a ponta).

As peças já existem separadas: o mascaramento (4), a camada de LLM (5), a busca nas políticas (8), o antifraude (10),
as ações e as aprovações (11). Falta o que as liga numa conversa: o grafo do Deli, que recebe a mensagem do cliente,
decide o caminho e só deixa uma ação acontecer depois da política, do antifraude e, se preciso, de uma pessoa. Sem
ele, nenhum dos sete cenários de `docs/fluxos-do-agente.md` roda, e a API (13), os evals (15) e a demo dependem disso.

**Ajuste de ordem:** o roadmap punha a change 9 (`classificador-intencao`) antes desta. A 9 serve o vencedor da change
7 (`pesquisa-intencao`), que ainda espera o teste escrito por pessoas e o treino na GPU. O agente só precisa da
**porta** do classificador, não do vencedor. Por isso a porta `IntentClassifier` e a rede de segurança por
palavras-chave vêm para esta change, e a 9 fica com o adaptador do vencedor, o `manifest.json`, o `/ready` e a porta
do CI. O caminho crítico deixa de esperar a pesquisa.

## What Changes

Escopo copiado do roadmap (seção 12), com o ajuste acima:

- `agent/`, com o grafo de `arquitetura.md`, seção "Desenho do agente":
  - nós, roteamento e as quatro ferramentas de leitura (`get_order`, `get_delivery_timeline`,
    `get_customer_summary`, `search_policy`), com o escopo vindo do contexto de execução, nunca do LLM;
  - a `ActionProposal` como saída estruturada do `support_agent` (`create_agent` do LangChain, como subgrafo);
  - o `interrupt` em `await_approval`, com o `AsyncPostgresSaver` (durabilidade `sync`);
  - os limites: 8 chamadas de ferramenta por turno, 2 revisões depois de uma negativa da política, 15 turnos por
    conversa e **60 s de prazo total por turno**; ao estourar, transferência para humano;
  - `compose_reply` e `validate_reply`, que confere valores e ações contra o resultado executado, remove link e
    imagem fora da lista permitida e usa `privacy.find_personal_data` na saída do LLM;
  - `mask_input` com `privacy.mask_text` e o `MarkerLedger` no estado; a chave do mascaramento numa variável de
    ambiente, com pelo menos 32 bytes e falha na subida se faltar;
  - os prompts `v1` (`support_agent`, `compose_reply`, `answer_faq`), montados por uma função única com blocos
    delimitados e escapados;
  - o nó `score_fraud`: lê o histórico com o `PostgresFraudContextReader`, monta o contexto com `claim_context`,
    pontua e preenche o `FraudAssessment` da aprovação. Uma `FraudScoringError` manda o caso para revisão humana
    (*fail closed*), nunca para execução automática.
- `bootstrap.py` (composition root): monta o `LlmRuntime` na subida com os papéis em uso, entrega os dois modelos de
  `RoleModels` ao `ModelFallbackMiddleware` do agente, carrega o modelo de fraude (`load_fraud_model`) e liga as
  portas. O `ModelRetryMiddleware` do agente também passa pelo governor, o que esta change confere com teste.
- **Vindo da change 9:** `intent/` com a porta `IntentClassifier` (rótulo, probabilidades calibradas, confiança e
  limiar), o enum `Intent` das 11 intenções (que sai de `datagen` e passa a ser importado por ele) e a **rede de
  segurança por palavras-chave**, que lê o texto de `privacy.normalize_text` e força o caminho sensível qualquer que
  seja a nota do classificador. Um classificador falso, configurável por mensagem, serve aos testes.
- O caminho sensível não chama nenhum LLM externo: resposta em texto fixo e transferência com prioridade alta.
- Um sinal de injeção (heurística simples) registra `security.injection_suspected`, sem mudar o caminho: detecção é
  sinal, não barreira (`seguranca.md`, 1.3).
- Dependências novas: `langgraph`, `langchain` (o `create_agent` e os middlewares), `langgraph-checkpoint-postgres`
  e o `psycopg` 3 com pool, que o checkpointer exige. As tabelas do checkpointer entram por migration do Alembic.

**Fora de escopo:**

- a API (conversas, SSE, lease, rotas de aprovação), a tabela de mensagens e a criação de conversas: change 13;
- o adaptador do classificador vencedor, o `manifest.json`, o `/ready` e a porta de F1 do CI: change 9;
- qualquer ferramenta que escreve, que não existe por desenho;
- os evals com cliente simulado e o juiz: changes 15 e 16;
- traces do Langfuse e do OpenTelemetry: change 14 (aqui ficam só os eventos de log);
- expiração de aprovação, reversão de ação e resgate de cupom.

**Pronto quando** (copiado do roadmap):

- os sete cenários de `fluxos-do-agente.md` passam no pytest com o LLM falso e sem rede;
- retomar duas vezes a mesma aprovação gera um só reembolso;
- o caminho sensível não chama nenhum LLM;
- com o LLM falso travando, o turno estoura o prazo e cai no modo degradado sem ação;
- o LLM falso pedindo o pedido de outro cliente recebe o pedido do contexto;
- uma conversa real de ponta a ponta funciona com o Gemini e com o Groq (`-m llm`).

Acrescentado: as palavras-chave levam ao caminho sensível mesmo com o classificador falso errando (critério que era da
change 9 e veio com a rede).

## Capabilities

### New Capabilities

- `agente`: o grafo de atendimento do Deli: mascaramento da entrada, roteamento pela intenção, investigação só com
  ferramentas de leitura no escopo do contexto, proposta estruturada, política, antifraude, aprovação humana com
  pausa e retomada, execução idempotente, resposta validada, limites, prazo do turno e modo degradado.
- `classificacao-intencao`: a porta do classificador de intenção, as 11 intenções e a rede de segurança por
  palavras-chave do caminho sensível. A change 9 acrescenta o adaptador do vencedor a esta mesma capability.

### Modified Capabilities

Nenhuma: as ações, as aprovações, o antifraude, a busca e a camada de LLM são usados como estão especificados.

## Impact

- **Código novo:** `src/deliverius/agent/`, `src/deliverius/intent/`, `src/deliverius/bootstrap.py`; a migration das
  tabelas do checkpointer; testes em `tests/unit/agent/`, `tests/unit/intent/`, `tests/integration/agent/` e
  `tests/llm/`.
- **Código alterado:** `datagen` importa o `Intent` de `intent/`; `config.py` e `.env.example` ganham a chave do
  mascaramento, o limiar do roteador e os limites do agente; o import-linter ganha `intent` e `agent` nas camadas e
  o dono de `langgraph`, `langchain` e `psycopg`.
- **Dependências:** `langgraph`, `langchain`, `langgraph-checkpoint-postgres`, `psycopg[binary]` e `psycopg-pool`.
- **Banco:** só as tabelas do checkpointer do LangGraph (aditivo).
- **Documentos:** `roadmap.md` (seções 9 e 12, tabela e grafo de dependências), `arquitetura.md`,
  `decisoes-arquitetura.md`, `robustez.md` (eventos e limites do agente) e o README.
