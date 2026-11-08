## Why

Change 2 do `docs/roadmap.md` (`nucleo-dominio`, fase F1), que cobre o requisito **R01** (engenharia de software
sólida) e prepara o terreno para R12 e R22. Todas as changes seguintes (política, mascaramento, gerador de dados,
ações, agente) precisam do mesmo vocabulário de domínio (dinheiro, IDs, tempo, erros), das tabelas do pedido e de
um acesso ao banco que **não deixa um cliente ver o dado de outro**. Fazer isso agora, antes de existir qualquer
rota ou ferramenta, faz o escopo de acesso nascer obrigatório em vez de ser lembrado depois.

## What Changes

Escopo copiado do roadmap:

- `shared/`: `Money` em centavos inteiros, IDs UUID v7, `Clock` injetável e erros base (`DeliveriusError`, que
  passa a ser a base do `DatabaseUnavailableError` criado na change 1), mais o `AccessScope` usado por todos os
  repositórios.
- `orders/`: cliente, restaurante, pedido, item, entrega e eventos da entrega, com o repositório (porta de leitura,
  implementação no Postgres e uma em memória que cumpre o mesmo contrato).
- Tabelas e migrations de `docs/modelo-dominio.md`, seção 2, **só as do domínio de pedidos e a auditoria**:
  `customers`, `restaurants`, `orders`, `order_items`, `delivery_events` e `audit_log`, com o trigger que recusa
  `UPDATE` e `DELETE`, as restrições que são a última barreira (`CHECK` de valores, chaves estrangeiras, inclusive
  a que impede um pedido de apontar para um cliente de outra sandbox) e os índices de `sandbox_id` e `customer_id`.
- `AccessScope` obrigatório em todo método de repositório que toca dado de cliente (`docs/autorizacao.md`,
  seção 4).
- Pool de conexões, `statement_timeout`, `idle_in_transaction_session_timeout` e sessão por unidade de trabalho
  (`docs/robustez.md`, seções 3 e 8).
- structlog configurado (JSON em produção, legível no desenvolvimento), com eventos `<módulo>.<evento>` e um filtro
  de mascaramento plugável que já remove chaves sensíveis conhecidas (`docs/robustez.md`, seção 5).
- Contratos novos do import-linter para `shared`, `orders` e `observability`.

## Fora de escopo

- As tabelas de ações e aprovações (change 11) e de conversas, mensagens e `llm_calls` (changes 5 e 13).
- O mascaramento de texto livre (`privacy/`, change 4): aqui o filtro de log só remove chaves conhecidas e recebe o
  mascarador real depois.
- Rotas da API sobre pedidos (change 13) e o teste da matriz de autorização, que nasce com a primeira rota
  protegida.
- A tabela `sandboxes`, a criação e a limpeza de sandboxes (change E2). Aqui `sandbox_id` é só uma coluna
  obrigatória; a chave estrangeira para `sandboxes` entra em E2, de forma aditiva.
- Escrita de pedidos pela aplicação (o gerador de dados, change 6, insere os dados) e o resumo do histórico do
  cliente para o antifraude (change 10).
- `trace_id` preenchido nos logs e na auditoria (change 14); a coluna já nasce, anulável.

## Capabilities

### New Capabilities

- `pedidos`: leitura de pedidos, itens e linha do tempo da entrega sempre limitada pelo escopo de acesso (sandbox e
  cliente), com valores em centavos e restrições de integridade no banco.
- `auditoria`: registro só de inserção de quem fez o quê, que o banco protege contra alteração e remoção.

### Modified Capabilities

Nenhuma. O `health-check` não muda de comportamento: o `DatabaseUnavailableError` só ganha uma classe base.

## Critério de pronto (do roadmap)

- `Money` tem testes de propriedade (Hypothesis);
- os repositórios passam nos testes de integração;
- o teste prova que um `UPDATE` (e um `DELETE`) no `audit_log` falha;
- um teste prova que o repositório não devolve o pedido de outro cliente nem de outra sandbox;
- uma consulta acima do `statement_timeout` é interrompida.

## Impact

- **Código novo:** `src/deliverius/shared/`, `src/deliverius/orders/`, `src/deliverius/observability/`,
  `src/deliverius/db/` (tabelas ORM e sessão), migration `0002`.
- **Código alterado:** `db/engine.py` (pool e tempos limite), `config.py` (novas variáveis), `api/main.py` (logs
  configurados na subida), `migrations/env.py` (metadados das tabelas), `pyproject.toml` (contratos do
  import-linter).
- **Dependências novas:** `structlog` (logs) e `uuid-utils` (UUID v7, que o Python 3.13 não tem na biblioteca
  padrão).
- **Variáveis novas no `.env.example`:** tamanho do pool, tempos limite do banco, formato e nível do log.
- **Documentação:** `docs/decisoes-arquitetura.md` (decisões novas), `docs/glossario.md` (termos novos) e
  `docs/roadmap.md` (a change passa a ter duas specs: `pedidos` e `auditoria`).
