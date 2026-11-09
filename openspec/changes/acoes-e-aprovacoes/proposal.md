## Why

Change 11 do `docs/roadmap.md` (`acoes-e-aprovacoes`, fase F3), que cobre os requisitos **R12** (agente que age) e
**R22** (segurança de LLM). O motor de política (change 3) só decide; falta quem **execute** o reembolso, o cupom e
o cancelamento sem duplicar, sem passar do teto quando duas operações correm juntas, e quem guarde a **decisão
humana** de forma que dois revisores não decidam a mesma coisa. É a última peça de escrita antes do agente
(change 12), que só propõe e depende dela para agir.

## What Changes

Escopo copiado do roadmap:

- `actions/`: reembolso, cupom e cancelamento com chave de idempotência e auditoria na mesma transação, com o pedido
  travado (`FOR UPDATE`) e a **política reavaliada na hora da execução**.
- `approvals/`: criar, listar e decidir (com trava otimista e motivo obrigatório; a mesma decisão repetida é
  idempotente), uma aprovação pendente por conversa (índice único parcial), mais a transferência com resumo
  estruturado.

Detalhado a partir do escopo (o roadmap foi atualizado com estes pontos):

- **Tabela `conversations` mínima** (sandbox, cliente, pedido, status), criada aqui porque ações, aprovações e
  transferências apontam para ela e a regra "uma pendente por conversa" precisa dela. As rotas de conversa, a lease
  do turno e as mensagens continuam na change 13.
- **Trava do cliente, além da do pedido:** o limite de R$ 100 autoaprovados em 30 dias soma pedidos diferentes, e
  só a trava do pedido não impede dois reembolsos automáticos em pedidos diferentes de passarem juntos.
- **Cancelamento muda o pedido:** status `cancelled` e evento na linha do tempo, na mesma transação da ação.
- **Aprovação decidida é imutável no banco:** um trigger recusa alterar uma aprovação que já saiu de `pending`.
- **Transferência:** criar (uma aberta por conversa), listar a fila por prioridade e assumir (dois revisores
  assumindo juntos → um vence). O resumo é recusado se trouxer dado pessoal.
- Migration `0005` com as quatro tabelas, índices de toda chave estrangeira e dos filtros das filas, e os triggers.
- Contrato do import-linter: `actions` e `approvals` na camada de capacidades, sem importar um ao outro.
- Porta no CI: 100% de cobertura de ramos em `actions`.

## Fora de escopo

- Rotas HTTP, JWT, modelos de resposta por papel, lease do turno, mensagens e o registro de qual revisor abriu qual
  conversa (change 13). Por isso não há linha nova no teste da matriz de autorização; o escopo de acesso é
  garantido nos repositórios.
- O grafo, os nós `request_approval`, `await_approval` e `execute_action` e a retomada com `Command(resume=...)`
  (change 12). Aqui ficam os serviços que esses nós chamam.
- A nota de fraude: a aprovação tem colunas para a nota e os fatores, preenchidas por quem chama; o modelo é da
  change 10.
- Expirar aprovações pendentes por prazo, reverter uma ação executada e o resgate do cupom (o cupom é a ação
  registrada, sem código nem carteira).
- Integração com meio de pagamento: o estorno é a ação gravada; não há gateway.
- O *outbox* para o webhook do n8n (extra E7).

## Capabilities

### New Capabilities

- `acoes`: execução idempotente e auditada de reembolso, cupom e cancelamento, com trava do cliente e do pedido e a
  política reavaliada na execução.
- `aprovacoes`: pedidos de aprovação humana (criar, listar, decidir com trava otimista e motivo) e transferências
  para humano com resumo estruturado; o status da conversa acompanha os dois.

### Modified Capabilities

Nenhuma. A auditoria (`auditoria`) e a leitura de pedidos (`pedidos`) são usadas como estão; a escrita no pedido
(cancelamento) é requisito de `acoes`.

## Impact

- Código novo: `src/deliverius/actions/`, `src/deliverius/approvals/`; métodos de escrita com trava em
  `orders/` (travar cliente e pedido, marcar cancelado); `db/tables.py` e `migrations/versions/..._0005_...`.
- `pyproject.toml`: contratos do import-linter e a porta de cobertura de `actions` no CI (`.github/workflows/ci.yml`).
- Docs: `modelo-dominio.md` (colunas e status das tabelas), `robustez.md` (ordem das travas, trava do cliente),
  `decisoes-arquitetura.md`, `arquitetura.md` (contrato entre aprovações e ações), `roadmap.md`, README (estado da
  change).
- Sem dependência nova, sem variável de ambiente nova, sem LLM.
