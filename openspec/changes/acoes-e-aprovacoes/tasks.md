## 1. Preparação

- [x] 1.1 `shared/ids.py`: `ConversationId`, `ActionId`, `ApprovalRequestId`, `HandoffId`, `ReviewerId`; `ConflictError` com `code` estável (padrão `conflict`) e testes; nenhum uso atual quebra
- [x] 1.2 Criar `src/deliverius/actions/` e `src/deliverius/approvals/`; import-linter: os dois na camada de `llm | retrieval` (sem se importarem) e nas fontes dos contratos de provedor de LLM e de `langchain_core`; `lint-imports` passa
- [x] 1.3 `policy/keys.py`: `proposal_key(conversation_id, order_id, action)` com JSON canônico versionado, itens ordenados e somados por ID; testes: mesma chave com itens em outra ordem, chave diferente para valor, tipo, motivo, pedido ou conversa diferentes, valor conhecido fixado (hash dourado); 100% dos ramos de `policy` continuam

## 2. Banco

- [x] 2.1 Migration `0005`: `conversations`, `approval_requests`, `handoffs`, `actions` com os `CHECK`, as `UNIQUE`, os índices parciais, a coluna gerada `priority_rank`, as FKs compostas e um índice para cada FK e filtro do design; `lock_timeout` no início; `db/tables.py` com `lazy="raise"`; o teste de migrations faz ida e volta
- [x] 2.2 Triggers: `approval_requests` recusa `UPDATE` e `DELETE` de linha já decidida; integração: alterar o status ou o motivo de uma aprovada falha, alterar uma pendente funciona
- [x] 2.3 Teste que mantém as listas literais dos `CHECK` (`kind`, `reason`, `approved_by`, status de conversa, aprovação e transferência) iguais aos `StrEnum`
- [x] 2.4 Integração das barreiras: valor zero recusado; ação com `approved_by = 'reviewer'` sem aprovação recusada; ação apontando aprovação pendente, de outra conversa ou de outra proposta recusada pela FK; ação com pedido diferente do da conversa recusada; segunda pendente na mesma conversa recusada

## 3. Pedidos: travas e cancelamento

- [x] 3.1 `orders`: porta `OrderLocker` e, no `PostgresOrderRepository`, `lock_customer(scope)` (só o `id`, `FOR NO KEY UPDATE`: serializa as execuções sem bloquear quem insere linhas que apontam para o cliente), `lock_order(scope, order_id)` (com itens e eventos, `FOR UPDATE OF orders`) e `mark_cancelled(order_id, at)` (confere `can_transition`, insere o evento `cancelled` com ator `system`); versão em memória para os testes unitários
- [x] 3.2 Integração: escopo de outro cliente e de outra sandbox → `None`; a segunda transação que trava o mesmo cliente espera a primeira; `lock_timeout` estourado vira `DatabaseTimeoutError`; `mark_cancelled` num pedido `out_for_delivery` dá `InvariantError`

## 4. Ações

- [x] 4.1 `actions/models.py` e `ports.py`: `ExecutionRequest` (conversa, pedido, proposta, `claimed_at`, autoridade), `Automatic | ReviewerApproved`, `Executed(action, replayed) | NotExecuted(decision)`, `ExecutedAction`, porta `ActionStore`
- [x] 4.2 `actions/repository.py`: histórico (30 dias do cliente + tudo do pedido) como `CustomerHistory`, busca por chave, inserção com tradução das restrições (`IntegrityError` pelo nome: chave única → repetição, FK da aprovação → `ConflictError("action_approval_mismatch")`), leitura por pedido com escopo; `ActionStore` em memória para os testes unitários
- [x] 4.3 `actions/service.py`: `ActionExecutor.execute` com trava do cliente → pedido, repetição, reavaliação, gravação, cancelamento e auditoria numa transação; `DatabaseTimeoutError` da trava → `ConflictError("action_lock_timeout")`; relógio injetável para `executed_at`; eventos de log do design
- [x] 4.4 Unitários com fakes, cobrindo 100% dos ramos: permitida, precisa de aprovação sem e com revisor, negada com revisor, repetição, cancelamento, conflito de trava, aprovação que não bate, conversa de outro pedido, prazo contado do `claimed_at` (23 h + 2 h de espera)
- [x] 4.5 Propriedade (Hypothesis, repositório em memória): para qualquer sequência de propostas sobre um pedido, a soma gravada nunca passa do total, e a soma autoaprovada do cliente em 30 dias nunca passa do limite
- [x] 4.6 Integração, corridas (`asyncio.gather`, 20 rodadas por teste): mesma proposta junta → 1 ação e 1 auditoria; dois reembolsos diferentes cuja soma passa do teto → 1 gravado, o outro `exceeds_ceiling`; dois automáticos em pedidos diferentes passando de R$ 100 → 1 gravado, o outro `exceeds_customer_limit`; aprovação retomada enquanto outro reembolso sai → a reavaliação nega
- [x] 4.7 Integração, falha no meio: tarefa cancelada depois da inserção e antes do commit → nem ação, nem auditoria, nem mudança no pedido
- [x] 4.8 Integração, escopo e consulta: pedido de outro cliente → `NotFoundError` e nada gravado; leitura das ações por pedido só no escopo; `EXPLAIN` do histórico usa os índices `(sandbox_id, customer_id, executed_at)` e `(order_id, executed_at)`
- [x] 4.9 Porta no CI: 100% de cobertura de ramos em `deliverius.actions` (mesmo mecanismo de `policy` e `privacy`)

## 5. Aprovações

- [ ] 5.1 `approvals/models.py` e `ports.py`: `ApprovalRequest`, `ApprovalStatus`, `Decision`, `FraudAssessment` (nota entre 0 e 1, versão, fatores), portas
- [ ] 5.2 `approvals/repository.py`: criar com `ON CONFLICT` e tradução do índice parcial, `get` com escopo de cliente ou sandbox, fila por cursor `(created_at, id)` com no máximo 50, decisão por `UPDATE` condicional, status da conversa na mesma transação
- [ ] 5.3 `approvals/service.py`: `ApprovalService.request`, `get`, `pending_queue` e `decide` (motivo com `strip`, de 1 a 500 caracteres; releitura para separar não encontrado, repetição, `approval_already_decided` e `approval_version_stale`); auditoria sem o texto do motivo; eventos de log
- [ ] 5.4 Unitários com fakes de cada ramo do serviço, incluindo motivo vazio, só com espaços e com 501 caracteres
- [ ] 5.5 Integração, corridas: dois revisores com decisões opostas juntos → 1 decisão e 1 `approval_already_decided`; dois revisores com a mesma decisão juntos → os dois recebem o estado aprovado e há 1 auditoria; criação repetida junta → 1 pedido; duas propostas diferentes juntas na mesma conversa → 1 pendente e 1 `approval_already_pending`
- [ ] 5.6 Integração, escopo e fila: revisor de outra sandbox → `NotFoundError`; cliente lê só a da própria conversa; 60 pendentes → páginas de 50 e 10 sem repetir nem pular; `EXPLAIN` da fila usa o índice parcial; status da conversa: `open` → `awaiting_approval` → `open`

## 6. Transferências

- [ ] 6.1 `HandoffSummary` (intenção, fatos, ações feitas, com limites) e `Priority`; `find_personal_data` em cada texto; marcadores do mascaramento aceitos
- [ ] 6.2 Repositório e `HandoffService`: criar com `ON CONFLICT` no índice parcial, fila por `(priority_rank DESC, created_at, id)`, assumir com `UPDATE` condicional e releitura; conversa `transferred`; auditoria e eventos de log
- [ ] 6.3 Unitários: resumo com CPF, telefone e e-mail recusado; limites de tamanho; ramos do assumir
- [ ] 6.4 Integração: criação repetida junta → 1 transferência; dois revisores assumindo juntos → 1 responsável e 1 `handoff_already_assigned`; fila com alta antes de normal, paginada; `EXPLAIN` da fila; outra sandbox → `NotFoundError`

## 7. Fluxos do lado da escrita

- [ ] 7.1 Integração com os estados finais de `fluxos-do-agente.md`, chamando os serviços na ordem que o grafo vai chamar: F1 (1 reembolso de R$ 8,00, 1 auditoria, nenhuma aprovação), F2a (pedido → aprovado → executado duas vezes → 1 reembolso de R$ 186,00 e a auditoria do revisor), F2b (recusado → nenhum reembolso e a auditoria da recusa), F7 (R$ 74,00 negado, R$ 12,00 executado), F5 (1 transferência de prioridade alta, nenhuma ação)

## 8. Documentação

- [ ] 8.1 `docs/modelo-dominio.md`: colunas, restrições e status de `conversations`, `actions`, `approval_requests` e `handoffs`
- [ ] 8.2 `docs/robustez.md`: a trava do cliente na corrida dos reembolsos, a linha "automáticos em pedidos diferentes" e a ordem das travas (cliente → pedido; linha própria → conversa)
- [ ] 8.3 `docs/decisoes-arquitetura.md`: chave derivada da proposta, trava do cliente, FK composta para a aprovação aprovada, aprovação imutável por trigger, aprovação cobre motivos novos; `docs/arquitetura.md`: o contrato entre `approvals` e `actions` passando pelo agente
- [x] 8.4 `docs/roadmap.md`: a tabela `conversations` mínima e a trava do cliente no escopo da change 11
- [ ] 8.5 README: change 11 no status e os números de testes atualizados

## 9. Verificação

- [ ] 9.1 `uv run ruff format --check .`
- [ ] 9.2 `uv run ruff check .`
- [ ] 9.3 `uv run mypy`
- [ ] 9.4 `uv run lint-imports`
- [ ] 9.5 `uv run pytest` (cobertura mínima de 85% e 100% dos ramos em `policy`, `privacy` e `actions`)
- [ ] 9.6 `uv run pytest -m integration --no-cov`
- [ ] 9.7 `openspec validate acoes-e-aprovacoes --strict`
