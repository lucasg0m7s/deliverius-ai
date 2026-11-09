## Context

O motor de política (`policy.evaluate`) decide uma proposta de forma pura: recebe a proposta, o pedido, o histórico
do cliente e o instante da reclamação, e devolve `Allowed`, `NeedsApproval` ou `Denied`. Falta o lado que escreve.
Ele tem três exigências que o motor sozinho não cumpre:

- **concorrência:** duas execuções podem passar no teto cada uma sozinha e, juntas, estourar;
- **repetição:** o grafo (change 12) reexecuta nós na retomada, e a mesma proposta não pode virar duas ações;
- **decisão humana:** dois revisores podem decidir ao mesmo tempo, e uma decisão gravada não pode mudar depois.

O que já existe e é reaproveitado:

- `orders.PostgresOrderRepository`, com o escopo no `WHERE`;
- `db.audit.record`, que grava na transação de quem chama;
- `shared.AccessScope`;
- `db.errors.translate_db_errors`;
- o `lock_timeout` de 2 s e o `statement_timeout` de 5 s, já aplicados a toda conexão por `db.engine`;
- `privacy.find_personal_data`.

Ainda não existe nenhuma tabela de conversa. As ações, as aprovações e as transferências apontam para uma conversa,
então a tabela mínima nasce aqui.

## Goals / Non-Goals

**Goals:**

- `actions.ActionExecutor.execute`, idempotente e com a política reavaliada dentro da trava do cliente e do pedido.
- `approvals.ApprovalService`: criar, ler, listar a fila e decidir, com trava otimista e motivo obrigatório.
- `approvals.HandoffService`: criar, listar a fila e assumir.
- Barreiras no banco que seguram o erro de código: chave única, índice único parcial, FK composta da ação para a
  aprovação aprovada, trigger de imutabilidade da decisão e `CHECK` em valores e status.
- Um teste de integração para cada corrida da seção 1 de `robustez.md` que esta change cobre.

**Non-Goals:**

- Rotas, JWT, lease do turno, mensagens da conversa (change 13).
- Nós do grafo e retomada com `Command(resume=...)` (change 12).
- Calcular a nota de fraude (change 10).
- Expirar aprovações, reverter ações, resgatar cupons, falar com um gateway de pagamento.

## Decisions

### Estrutura

```
src/deliverius/
├── shared/ids.py          + ConversationId, ActionId, ApprovalRequestId, HandoffId, ReviewerId
├── shared/errors.py       ConflictError ganha um `code` estável (padrão: "conflict"), como o `rule` do InvariantError
├── policy/keys.py         proposal_key(conversation_id, order_id, action): a chave da proposta (pura)
├── orders/                + OrderLocker (porta) e métodos no PostgresOrderRepository:
│                            lock_customer, lock_order (FOR UPDATE, com escopo), mark_cancelled
├── actions/
│   ├── models.py          ExecutionRequest, Automatic | ReviewerApproved, Executed | NotExecuted, ExecutedAction
│   ├── ports.py           ActionStore (histórico, busca por chave, inserção, leitura por pedido)
│   ├── repository.py      PostgresActionStore
│   └── service.py         ActionExecutor
├── approvals/
│   ├── models.py          ApprovalRequest, ApprovalStatus, Decision, FraudAssessment, Handoff, HandoffSummary, Priority
│   ├── ports.py           ApprovalStore, HandoffStore
│   ├── repository.py      PostgresApprovalStore, PostgresHandoffStore (inclui o status da conversa)
│   └── service.py         ApprovalService, HandoffService
└── db/tables.py           + ConversationRow, ActionRow, ApprovalRequestRow, HandoffRow
migrations/versions/2026_11_09_0005_actions_approvals_handoffs.py
```

Import-linter: `deliverius.actions | deliverius.approvals` entram na camada de `llm | retrieval`. Como estão na
mesma linha do contrato de camadas, não se importam. As duas entram também nas fontes dos contratos de provedor de
LLM e de `langchain_core`.

### A chave da proposta, num lugar só

`policy.proposal_key(conversation_id, order_id, action)` devolve o SHA-256, em hexadecimal, de um JSON canônico:
`{"v": 1, "conversation": ..., "order": ..., "kind": ..., "reason": ..., "amount_cents": ..., "items":
[[order_item_id, quantity], ...]}`, com os itens ordenados pelo ID e as chaves ordenadas.

- A mesma função vale para a ação (`idempotency_key`) e para a aprovação (`proposal_key`). Por isso fica em
  `policy`: os dois módulos já dependem dele e não podem depender um do outro.
- O campo `v` permite mudar a forma da chave sem colidir com as antigas.
- Itens repetidos com o mesmo ID são somados antes. Assim, a mesma reclamação escrita de duas formas gera a mesma
  chave.

**Alternativa:** chave aleatória gerada no grafo e guardada no estado. Ela também funciona na retomada, mas não
pega a mesma proposta refeita pelo agente num turno novo. Com a chave derivada do conteúdo, o banco recusa as duas.

### Executar: trava, repetição, reavaliação, gravação

`ActionExecutor.execute(scope: CustomerScope, request: ExecutionRequest) -> Executed | NotExecuted`, numa
transação curta (`async with factory() as session, session.begin()`):

1. `lock_customer(scope)`: `SELECT id FROM customers WHERE id = :c AND sandbox_id = :s FOR UPDATE`. A consulta lê só
   o `id`, nenhum dado pessoal. Sem linha → `NotFoundError`.
2. `lock_order(scope, order_id)`: o pedido com itens e eventos, `FOR UPDATE OF orders`, com o escopo no `WHERE`.
   Sem linha → `NotFoundError`, a mesma resposta de "não existe".
3. A chave é calculada e buscada. Se a ação já existe → `Executed(action, replayed=True)`, sem auditoria nova.
4. O histórico é lido: ações do cliente com `executed_at >= claimed_at − 30 dias`, mais todas as ações do pedido. É
   o contrato do `CustomerHistory`.
5. `policy.evaluate(proposta, pedido, histórico, claimed_at)`:
   - `Denied` → `NotExecuted(decision)` e o log `actions.reevaluation_denied`;
   - `NeedsApproval` com a autoridade `Automatic` → `NotExecuted(decision)` e o log `actions.needs_approval`;
   - `NeedsApproval` com `ReviewerApproved` → segue;
   - `Allowed` com qualquer autoridade → segue.
6. A ação é inserida. Se for um cancelamento, `mark_cancelled` muda o status (conferido com `can_transition`) e
   insere o evento `cancelled` com o ator `system`. A auditoria `actions.executed` vai na mesma transação.

**Por que travar o cliente, e não só o pedido:** o limite de R$ 100 em 30 dias (regra R3) soma ações de pedidos
diferentes. Só com a trava do pedido, duas execuções automáticas em pedidos diferentes leem o mesmo histórico e as
duas passam. A trava do cliente serializa tudo o que esse cliente executa; o custo é zero na prática, porque um
cliente raramente tem duas execuções no mesmo segundo.

**Ordem fixa das travas:** cliente → pedido. Nenhuma outra transação desta change trava o cliente ou o pedido, e as
de aprovação e de transferência só tocam a própria linha e depois a conversa. A ordem fica registrada em
`robustez.md`, seção 8.

**Trava que não vem:** o `lock_timeout` de 2 s da conexão estoura com o SQLSTATE `55P03`, que
`translate_db_errors` já transforma em `DatabaseTimeoutError`. O serviço o converte em
`ConflictError("action_lock_timeout")` e registra `actions.lock_conflict`. Quem chama pode repetir, porque a
execução é idempotente.

**O instante da reclamação vem de quem chama.** Na aprovação, ele fica guardado em `claimed_at`, e o agente o repassa
na retomada. O pedido e o histórico são os atuais. Assim, uma aprovação decidida duas horas depois não perde o
prazo de 24 h, mas também não ignora um reembolso que outra conversa fez nesse meio tempo.

**A aprovação humana cobre os motivos novos.** Se a reavaliação dá `NeedsApproval` por um motivo que não existia
quando o revisor decidiu (por exemplo, o limite do cliente estourou nesse meio tempo), a ação sai mesmo assim. Os
limites de autoaprovação existem para mandar o caso a uma pessoa, e uma pessoa disse sim. A auditoria grava os
motivos da reavaliação, então o caso fica visível. `Denied` nunca sai: teto, prazo e estado continuam valendo.
**Alternativa:** exigir que os motivos sejam os mesmos da decisão. Ela foi descartada porque devolveria ao revisor
um caso que ele já aprovou sabendo que passava do limite.

**Erro da última barreira:** `translate_db_errors` trata `IntegrityError` (subclasse de `DBAPIError`) como banco
indisponível. Por isso, o repositório captura antes o `IntegrityError` pelo nome da restrição:

- a chave única (só se o código errar, porque a trava já serializa) → nova leitura e repetição;
- a FK da aprovação → `ConflictError("action_approval_mismatch")`.

### A aprovação garantida pelo banco, sem um módulo importar o outro

`actions` e `approvals` estão na mesma camada. O agente (change 12) lê a aprovação decidida em `approvals` e passa
para `actions` a autoridade `ReviewerApproved(approval_request_id, reviewer_id)`. Para que um erro do chamador não
execute com a aprovação errada, a ação grava `approval_request_id`, `conversation_id`, `idempotency_key` e uma
coluna `approval_status`. Uma FK composta as liga a `approval_requests (id, conversation_id, proposal_key, status)`,
que tem uma `UNIQUE` com essas quatro colunas. `CHECK`:

- `approval_status IS NULL OR approval_status = 'approved'`;
- `(approved_by = 'reviewer') = (approval_request_id IS NOT NULL)`;
- `(approval_request_id IS NULL) = (approval_status IS NULL)`.

Na ação automática as colunas ficam nulas e a FK (`MATCH SIMPLE`) não se aplica. Uma ação do revisor só é gravada
se a aprovação apontada é dessa conversa, dessa proposta e está aprovada. O trigger de imutabilidade (abaixo)
garante que ela continue assim.

**Alternativas:**

- `actions` lê `approval_requests` direto pelo ORM. Funciona, mas acopla um módulo à tabela do outro, e o
  import-linter não pegaria esse acoplamento.
- Fundir os dois módulos num só. Isso mistura a fila do console com a execução, e a arquitetura já os separa.

### Aprovações

**Tabela `approval_requests`:**

| Coluna | Detalhe |
| --- | --- |
| Identificação | `id`, `sandbox_id`, `customer_id`, `conversation_id`, `order_id` |
| Proposta | `proposal_key`, `proposal` (JSONB: tipo, motivo, valor, itens), `claimed_at` |
| Decisão da política | `policy_reasons` (`text[]`), `policy_ceiling_cents`, `policy_document` |
| Fraude (anuláveis, preenchidas na change 12) | `fraud_score` (`CHECK` entre 0 e 1), `fraud_model_version`, `fraud_factors` (JSONB de `{feature, contribution}`) |
| Decisão humana | `status` (`pending`, `approved`, `rejected`), `version` (começa em 1), `decided_by`, `decided_at`, `decision_reason` (até 500 caracteres) |
| Datas | `created_at` |

Restrições:

- `UNIQUE (conversation_id, proposal_key)`: a criação idempotente;
- índice único parcial `(conversation_id) WHERE status = 'pending'`: uma pendente por conversa;
- `UNIQUE (id, conversation_id, proposal_key, status)`: o alvo da FK das ações;
- `CHECK`: status decidido ⇔ `decided_by`, `decided_at` e `decision_reason` preenchidos.

Índices:

- fila: `(sandbox_id, created_at, id) WHERE status = 'pending'`;
- chaves estrangeiras: `customer_id` e `order_id`.

**Criar** (`ApprovalService.request`):

1. `INSERT … ON CONFLICT (conversation_id, proposal_key) DO NOTHING RETURNING`.
2. Sem linha de volta → lê a existente e devolve (`approvals.request_replayed`).
3. Se a recusa vier do índice parcial (outra proposta pendente) → `ConflictError("approval_already_pending")`.
4. Com o pedido criado, `UPDATE conversations SET status = 'awaiting_approval' WHERE id = :c AND status = 'open'`, e a
   auditoria `approvals.requested`, tudo na mesma transação.

O `ON CONFLICT` com o alvo da chave única não cobre o índice parcial. A violação dele chega como `IntegrityError`
`23505` com o nome do índice e é traduzida.

**Decidir** (`ApprovalService.decide(scope: SandboxScope, approval_id, expected_version, decision, reason, reviewer_id)`):

1. O motivo passa por `strip`; vazio, ou com mais de 500 caracteres → `InvalidInputError`.
2. `UPDATE … SET status, decided_by, decided_at, decision_reason, version = version + 1 WHERE id = :id AND
   sandbox_id = :s AND status = 'pending' AND version = :v RETURNING`.
3. Sem linha de volta, uma releitura no escopo separa os casos:
   - não existe → `NotFoundError`;
   - já decidido com a mesma decisão → devolve o estado atual (`approvals.decision_replayed`);
   - já decidido com outra decisão → `ConflictError("approval_already_decided")`;
   - pendente com outra versão → `ConflictError("approval_version_stale")`.
4. Com sucesso, `UPDATE conversations SET status = 'open' WHERE id = :c AND status = 'awaiting_approval'`, e a
   auditoria `approvals.decided` com o ator `reviewer`, a decisão e a versão nova. O texto do motivo fica só na
   linha da aprovação. Ele é texto livre do revisor e pode conter dado pessoal, e a auditoria é guardada por anos
   sem poder ser alterada.

"Mesma decisão repetida" compara só a decisão, e não o revisor nem o motivo. Se dois revisores aprovam juntos, um
grava e o outro recebe o estado aprovado. É o resultado que ele queria, e reenviar o mesmo clique não pode virar
erro.

**Imutável depois de decidida:** um trigger `BEFORE UPDATE` recusa a alteração quando `OLD.status <> 'pending'`, e
um `BEFORE DELETE` recusa a remoção de linha decidida. A função segue o mesmo padrão do `audit_log`.

**Ler:**

- `get(scope, approval_id)`: o cliente lê a da própria conversa (`customer_id` no `WHERE`) e o revisor lê as da
  sandbox;
- `pending_queue(scope: SandboxScope, page)`: por cursor `(created_at, id)`, em ordem crescente, no máximo 50 itens.

Os modelos de resposta por papel, sem a nota de fraude para o cliente, são da change 13. Aqui, o domínio devolve
tudo e o teste só garante o escopo.

### Transferências

**Tabela `handoffs`:**

| Coluna | Detalhe |
| --- | --- |
| Identificação | `id`, `sandbox_id`, `customer_id`, `conversation_id` |
| Caso | `priority` (`normal` ou `high`), `summary` (JSONB) |
| Fila | `status` (`open` ou `assigned`), `assigned_to`, `assigned_at` |
| Datas | `created_at` |

Restrições e índices:

- índice único parcial `(conversation_id) WHERE status = 'open'`;
- fila: `(sandbox_id, priority_rank DESC, created_at, id) WHERE status = 'open'`, com `priority_rank` como coluna
  gerada (2 para `high`, 1 para `normal`), para o cursor ordenar sem `CASE`;
- `customer_id` com índice.

**O resumo** (`HandoffSummary`) tem:

- `intent`: código, `[a-z_]{1,40}`;
- `facts`: até 10 textos de até 300 caracteres;
- `actions_taken`: até 10 textos de até 200 caracteres.

Cada texto passa por `privacy.find_personal_data`. Se achar algo, a criação dá `InvalidInputError` e nada é
gravado. Os marcadores do mascaramento (`[CPF_1]`) não contam como dado pessoal. A regex é a do `privacy`, já
testada contra ReDoS, e o tamanho é conferido antes dela.

**Criar:**

1. `INSERT … ON CONFLICT DO NOTHING` sobre o índice parcial, com `ON CONFLICT (conversation_id) WHERE status = 'open'`.
2. Sem linha de volta → devolve a aberta (`approvals.handoff_replayed`).
3. Com a transferência criada, `UPDATE conversations SET status = 'transferred' WHERE id = :c AND status <>
   'closed'`, e a auditoria `approvals.handoff_created`.

**Assumir:**

1. `UPDATE … SET status = 'assigned', assigned_to, assigned_at WHERE id AND sandbox_id AND status = 'open'
   RETURNING`.
2. Sem linha de volta, uma releitura separa os casos:
   - não existe → `NotFoundError`;
   - assumida pelo mesmo revisor → estado atual;
   - assumida por outro → `ConflictError("handoff_already_assigned")`.

Uma transferência assumida não tem trigger de imutabilidade. A change 13 pode precisar de "devolver à fila", e não
há execução que dependa dela.

### Conversas (mínimo)

**Tabela `conversations`:**

| Coluna | Detalhe |
| --- | --- |
| Identificação | `id` (o `thread_id` do LangGraph), `sandbox_id`, `customer_id`, `order_id` (anulável) |
| Estado | `status` (`open`, `awaiting_approval`, `transferred`, `closed`) |
| Datas | `created_at` |

Restrições e índices:

- FK composta `(customer_id, sandbox_id)` para `customers`, como em `orders`;
- FK `order_id` para `orders`;
- `UNIQUE (id, customer_id, order_id)`, que é o alvo da FK composta de `actions`. Assim, uma ação só aponta para
  uma conversa do mesmo cliente e do mesmo pedido;
- índices em `(sandbox_id, customer_id, created_at)` e em `order_id`.

Esta change só muda o `status`. Criar a conversa, a lease e as mensagens são da change 13; os testes criam as
linhas por fixture.

### Ações

**Tabela `actions`:**

| Coluna | Detalhe |
| --- | --- |
| Identificação | `id`, `sandbox_id`, `customer_id`, `order_id`, `conversation_id` |
| Proposta | `kind`, `reason`, `amount_cents` (`CHECK > 0`), `items` (JSONB, `CHECK jsonb_typeof = 'array'`) |
| Idempotência | `idempotency_key` (`UNIQUE`) |
| Quem aprovou | `approved_by` (`automatic` ou `reviewer`), `approval_request_id`, `approval_status` |
| Datas | `claimed_at`, `executed_at` |

Chaves estrangeiras:

- `(customer_id, sandbox_id)` para `customers`;
- `order_id` para `orders`;
- `(conversation_id, customer_id, order_id)` para `conversations`;
- `(approval_request_id, conversation_id, idempotency_key, approval_status)` para `approval_requests`.

Índices:

- `(sandbox_id, customer_id, executed_at)`, para o histórico de 30 dias;
- `(order_id, executed_at)`, para o histórico do pedido e a leitura por pedido;
- `conversation_id` e `approval_request_id`, para as chaves estrangeiras.

Os valores de `kind`, `reason` e `approved_by` ficam em listas literais nos `CHECK`, e um teste os mantém iguais
aos `StrEnum` de `policy`.

A soma das ações de um pedido não cabe num `CHECK`, que só enxerga uma linha. Quem a garante é a reavaliação sob a
trava, provada por um teste de propriedade (Hypothesis, com o repositório em memória) e pelos testes de corrida.

### Eventos de log e auditoria

| Módulo | Eventos de log |
| --- | --- |
| `actions` | `executed` (tipo, valor, `approved_by`), `replayed`, `reevaluation_denied` (motivo), `needs_approval` (motivos), `lock_conflict`, `approval_mismatch` |
| `approvals` | `requested`, `request_replayed`, `pending_conflict`, `decided`, `decision_replayed`, `decision_conflict`, `version_stale`, `handoff_created`, `handoff_replayed`, `handoff_assigned`, `handoff_assign_conflict`, `handoff_rejected_personal_data` |

Os logs levam só IDs, códigos e valores; nunca o motivo do revisor nem o texto do resumo. `reevaluation_denied`,
`decision_conflict` e `version_stale` viram métricas no E1 (`observabilidade.md`).

Auditoria:

- `actions.executed`: ator `agent`; nos dados, tipo, motivo, valor, `approved_by`, `approval_request_id` e os
  motivos da reavaliação, quando houver;
- `approvals.requested`: ator `agent`;
- `approvals.decided`: ator `reviewer`, com o `reviewer_id`;
- `approvals.handoff_created`: ator `agent`;
- `approvals.handoff_assigned`: ator `reviewer`.

## Checklist de `docs/robustez.md`, seção 11

| Item | Resposta |
| --- | --- |
| Rota, ferramenta, `AccessScope` | Nenhuma rota nem ferramenta; as linhas da matriz entram na 13. Todo método de repositório recebe o escopo e filtra no `WHERE`: a execução exige `CustomerScope`, decidir e listar as filas exigem `SandboxScope`, ler aceita os dois. Objeto de outro escopo → `NotFoundError`. Testes de integração com outro cliente e outra sandbox em cada método |
| Corrida | (1) Mesma proposta junta → trava do cliente + chave única; (2) reembolsos diferentes no mesmo pedido → trava do pedido + reavaliação; (3) automáticos em pedidos diferentes → trava do cliente; (4) dois revisores → `UPDATE` condicional pela versão; (5) duas aprovações na conversa → índice parcial; (6) criação repetida junta → `ON CONFLICT`; (7) dois assumindo → `UPDATE` condicional. Cada uma tem teste com `asyncio.gather` no Postgres |
| Idempotência | Ação: `idempotency_key`. Aprovação: `(conversation_id, proposal_key)`. Decisão: a mesma decisão devolve o estado. Transferência: uma aberta por conversa. Assumir: o mesmo revisor recebe o estado |
| Timeout e novas tentativas | Sem chamada externa. Banco: `statement_timeout` de 5 s e `lock_timeout` de 2 s da conexão. Nenhuma nova tentativa dentro do serviço; o conflito de trava é repetível por quem chama |
| Recursos esgotáveis | Filas com `LIMIT` de 50 e cursor; resumo e motivo com tamanho máximo; o histórico lido é limitado por 30 dias e pelo pedido |
| Queda no meio | Cada operação é uma transação só (ação + pedido + auditoria; aprovação + conversa + auditoria). Cancelada ou caída, é desfeita inteira. `CancelledError` nunca é capturado sem relançar |
| Transação com rede ou LLM | Nenhuma |
| Eventos de log | Tabela acima; três viram métrica |
| Texto de fora e regex | Motivo do revisor e resumo da transferência: tamanho limitado e `find_personal_data` no resumo. Os dois só entram no banco como parâmetro, nunca em log. A proposta já chega como `ProposedAction` tipada |
| Grafo, banco, API | Migration aditiva (quatro tabelas novas, triggers, nenhuma coluna mexida), com `downgrade` testado. Sem grafo nem API |
| Consultas e índices | Índices acima, um por filtro e por FK. `EXPLAIN` registrado nos testes do histórico, da fila de aprovações e da fila de transferências, sem `Seq Scan` nas tabelas que crescem. Nenhuma listagem com N+1: a fila não carrega relacionamentos |
| Travas e migrations | Ordem cliente → pedido; aprovação e transferência travam a própria linha e depois a conversa. `lock_timeout` no início da migration; as tabelas são novas, sem trava em tabela com dados |
| Configuração nova | Nenhuma |

## Checklist de `docs/seguranca.md`, seção 7

- Nenhuma ação com efeito pula a política: a execução sempre reavalia, e a única porta de escrita é o
  `ActionExecutor`.
- O agente continua sem ferramenta que escreve. O `customer_id` e o `order_id` da execução vêm do escopo e da
  conversa, nunca do LLM, e a FK composta para `conversations` recusa um pedido que não seja o da conversa.
- Nenhum dado pessoal em log ou auditoria: o motivo do revisor fica fora dos dois, e o resumo da transferência é
  recusado se trouxer dado pessoal.
- Sem LLM, sem prompt, sem segredo novo.

## Avaliação

Não há LLM nem modelo nesta change. Os critérios de pronto são estes:

| O quê | Como se mede | Limiar |
| --- | --- | --- |
| Ramos de `actions` | `pytest --cov-branch` com porta no CI | 100% |
| Corridas | Testes de integração com `asyncio.gather`, cada um repetido 20 vezes no mesmo teste | Estado final certo em todas |
| Teto sob qualquer sequência | Hypothesis sobre o executor com repositório em memória | Soma ≤ total em todos os exemplos |
| Cenários F1, F2 (a e b) e F7 do lado da escrita | Integração: o estado final do banco de `fluxos-do-agente.md` | Igual ao esperado |

## Risks / Trade-offs

- [A trava do cliente serializa execuções de um mesmo cliente] → No volume da demo isso é imperceptível, e a trava
  dura uma transação curta, sem rede. Se um dia pesar, o limite de 30 dias vira um contador com `UPDATE`
  condicional, como o orçamento de LLM.
- [A FK composta para `approval_requests` usa `status` como parte da chave, e mudar o status exige uma trava mais
  forte que a das FKs] → Só uma aprovação já `approved` (imutável) é referenciada por ações, então nunca há
  disputa entre decidir e executar a mesma aprovação.
- [A aprovação humana cobre um motivo novo de `NeedsApproval`] → É uma decisão de produto escrita acima, com
  auditoria dos motivos; dá para endurecer depois sem migration.
- [`conversations` nasce aqui com só o status] → A change 13 acrescenta colunas (lease, contadores) de forma
  aditiva; os nomes e status seguem `modelo-dominio.md`.
- [Um pedido pendente pode ficar para sempre] → A expiração está fora de escopo e anotada; `robustez.md`, seção 7,
  já pede decidir ou expirar as pendentes antes de um deploy que mexa no grafo.

## Decisões humanas pendentes

Nenhuma bloqueia a implementação. Uma decisão de produto tomada aqui pode ser revista: a aprovação do revisor cobre
um motivo novo de "precisa de aprovação" que apareça entre a decisão e a execução (acima, com a alternativa).
