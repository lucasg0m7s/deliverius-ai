## 1. Números da política

- [x] 1.1 `policy/limits.py`: `PolicyLimits` com os campos e valores do design e `POLICY_LIMITS: Final`; `__post_init__` recusa percentual fora de 0–100, `minor_delay >= major_delay` e janela não positiva, com `InvariantError` de código estável; testes unitários de cada invariante e dos valores padrão iguais à tabela de `modelo-dominio.md`, seção 3

## 2. Modelos de entrada e de decisão

- [x] 2.1 `policy/models.py`: `ActionKind`, `ClaimReason`, `ClaimedItem`, `ProposedAction`, `ApprovedBy`, `PriorAction`, `CustomerHistory` (docstring com o contrato do histórico: ações do cliente na janela de 30 dias mais todas as do pedido), `PolicyDocumentId` (`POL-01` a `POL-12`), `DenialReason` e `ApprovalReason` com os códigos do design; `PriorAction.executed_at` aware (`require_aware`); testes de construção e da recusa de instante sem fuso
- [x] 2.2 `Allowed`, `NeedsApproval` (recusa `reasons` vazio com `InvariantError`) e `Denied`, e `type PolicyDecision`; teste de que um `match` com `assert_never` cobre as três variantes (o mypy confere)

## 3. Motor

- [x] 3.1 `policy/engine.py`, etapa 1 (forma): valor zero → `amount_not_positive`; combinação não coberta → `not_covered` sem documento; itens ausentes nas situações com item → `items_required`; itens presentes nas outras → `items_not_allowed`; testes de cada caso, inclusive o reembolso por atraso negado
- [x] 3.2 Etapa 2 (estado e prazo), com os instantes de partida do design e `claimed_at` aware: item faltando, errado e qualidade exigem entregue (`order_not_delivered`) e 24 h; atraso recusa cancelado e falha (`order_status_not_eligible`) e 24 h depois da entrega; não entregue em andamento → `order_in_progress`, 48 h do evento final; cobrança duplicada em 90 dias do `placed_at`; cancelamento depois de sair para entrega → `order_already_dispatched`; `InvariantError` para entregue sem `delivered_at` e falha sem evento; testes na borda exata de cada janela (no limite passa, um segundo depois expira)
- [x] 3.3 Etapa 3 (itens): item fora do pedido → `item_not_in_order`; quantidades repetidas somadas; quantidade menor que 1 ou maior que a restante → `invalid_quantity`; unidades com reembolso ou cupom anterior no mesmo pedido contam como reclamadas → `item_already_claimed`; atraso já compensado no pedido (cupom ou cancelamento por atraso) → `delay_already_compensated`; testes de cada caso e do item reclamado antes por outro motivo (R6)
- [x] 3.4 Etapa 4 (teto): item faltando e errado = preço × quantidade; qualidade = 50% com `Money.fraction`; atraso de 30 a 60 min = cupom de 10% do total limitado a R$ 15,00; mais de 60 min = cupom de R$ 20,00 ou cancelamento com o restante; abaixo de 30 min → `delay_below_minimum`; não entregue, cobrança duplicada e cancelamento = total menos o já reembolsado; valor acima → `exceeds_ceiling` com o teto; testes com os exemplos da spec (R$ 29,95, R$ 15,00, R$ 62,00) e F7 (R$ 74,00 negado com teto de R$ 12,00)
- [x] 3.5 Etapa 5 (sempre humano): `marked_as_delivered`, `duplicate_charge_unverified`, `restaurant_already_accepted` (aceito, em preparo, pronto para retirada); testes de cada caso, inclusive o pedido em preparo atrasado mais de 60 min, em que o cancelamento não exige o motivo do aceite
- [x] 3.6 Etapa 6 (limites): reembolso e cancelamento acima de R$ 50,00 → `exceeds_action_limit`; cupom acima de R$ 20,00 → `exceeds_coupon_limit`; soma dos autoaprovados nos 30 dias mais o valor acima de R$ 100,00 → `exceeds_customer_limit` (aprovados por revisor e cancelamentos de cliente não contam; ação de 31 dias atrás não conta; borda exata da janela); cancelamento antes do aceite isento; vários motivos juntos aparecem todos; testes de cada caso e de F2 (R$ 186,00 marcado como entregue → `marked_as_delivered` e `exceeds_action_limit`)
- [x] 3.7 `evaluate` encadeando as etapas na ordem do design, com `match` exaustivo sobre `ClaimReason`; testes de ponta a ponta de F1 (R$ 8,00 permitido, `POL-03`), F4a (R$ 500,00 negado) e do cancelamento antes do aceite de R$ 186,00 permitido com `POL-01`; função nenhuma passa de cerca de 30 linhas
- [x] 3.8 Propriedades com Hypothesis (estratégias de pedido, proposta e histórico em `tests/unit/policy/strategies.py`, reaproveitando `tests/support/order_builders.py`): não negada ⇒ valor ≤ teto; não negada e reembolso ou cancelamento ⇒ reembolsado no pedido + valor ≤ total; determinismo; monotonia do teto; histórico autoaprovado a mais nunca transforma "precisa de aprovação" em "permitida"; proposta permitida, depois de entrar no histórico, nunca é permitida de novo para os mesmos itens
- [x] 3.9 Teste de cobertura das regras: uma lista no teste liga cada linha das tabelas "Regras" e "Limites de autoaprovação" de `modelo-dominio.md`, seção 3, ao nome do teste que a prova; o teste falha se algum nome da lista não existir no módulo

## 4. Documentos da política

- [x] 4.1 `policy/docs/POL-01-cancelamento.md` a `POL-12-privacidade.md`, em português, título `# POL-xx · Nome` e seções `##` (quando se aplica, o que o Deliverius faz, prazo, quando uma pessoa analisa), com todo número como marcador (`{{auto_approval_limit}}`); POL-09 e POL-10 sem promessa de reembolso e com prioridade alta; POL-12 sem prazo numérico de retenção; nenhuma marca real
- [x] 4.2 `policy/documents.py`: `PolicyDocument`, `load_policy_documents()` com `importlib.resources` (exatamente doze, nome do arquivo igual ao título, pelo menos uma seção) e `render_policy_document(document, limits)` com a formatação por tipo do design; `PolicyDocumentError` com documento e marcador; testes: `POL-03` preenchido mostra `24 horas` e `R$ 50,00`; marcador desconhecido falha com o nome dos dois; todos os doze se preenchem sem erro; nenhum documento cru tem `R$` seguido de dígito nem número seguido de `%`, minuto, hora ou dia; todo campo de `PolicyLimits` usado na decisão aparece em pelo menos um documento
- [x] 4.3 `uv build` gera a wheel com os doze `.md` dentro de `deliverius/policy/docs/` (conferido listando o arquivo); se o `uv_build` não os incluir, ajustar a configuração do build e repetir

## 5. Fronteiras e CI

- [x] 5.1 `policy/__init__.py` com a API pública (`evaluate`, decisões, modelos, `PolicyLimits`, `POLICY_LIMITS`, documentos); testes importam só dela
- [x] 5.2 `pyproject.toml`: contrato "policy is pure", camadas `api → policy → orders` e `deliverius.policy` entre os proibidos dos utilitários; um import proibido de propósito (`import sqlalchemy` e `from deliverius import db` em `policy/engine.py`) quebra o `lint-imports` e é desfeito
- [x] 5.3 `branch = true` em `[tool.coverage.run]`; etapa nova no `ci.yml` que exige 100% dos ramos em `deliverius.policy` rodando `tests/unit/policy`, com a sintaxe conferida na documentação da versão instalada do `pytest-cov`; rodada local passa com 100% e falha ao comentar um teste de ramo (desfeito em seguida)

## 6. Documentação

- [x] 6.1 `docs/modelo-dominio.md`, seção 3: as regras R1–R8 do design (como foram decididas na revisão), o instante de partida de cada prazo, e que os documentos ficam em `src/deliverius/policy/docs/`
- [x] 6.2 `docs/decisoes-arquitetura.md`: motor recebe fatos e não repositório; união de três decisões com primeiro motivo de negativa e todos os de aprovação; números em código e não em variável de ambiente ou YAML; marcadores com regex própria e não Jinja2; documentos como dados do pacote; cobertura de ramos ligada no projeto e etapa de 100% em `policy`; com as alternativas
- [x] 6.3 `docs/glossario.md`: ação proposta no domínio (`ProposedAction`, diferente do `ActionProposal` do LLM), situação da reclamação (`ClaimReason`), teto (`ceiling`), histórico do cliente (`CustomerHistory`, `PriorAction`), limites da política (`PolicyLimits`), código de motivo (`DenialReason`, `ApprovalReason`)
- [x] 6.4 `docs/roadmap.md`: na change 3, o preenchimento dos marcadores e os critérios acrescentados; na change 8, "números preenchidos com `render_policy_document` (change 3)"; registro de que a change está proposta

## 7. Verificação

- [x] 7.1 `uv run ruff format --check .`, `uv run ruff check .`, `uv run mypy`, `uv run lint-imports`, `uv run pytest` (cobertura total ≥ 85% com ramos) e a etapa de 100% dos ramos em `policy`
- [x] 7.2 `uv run pytest -m integration --no-cov` continua verde (nenhum teste novo de integração: o motor não toca o banco; o teste de corrida dos dois reembolsos fica na change 11, como diz o design)
- [x] 7.3 Checklists conferidos: `docs/robustez.md`, seção 11 (respondido no design) e `docs/seguranca.md`, seção 7; matriz de autorização sem linha nova (nenhuma rota ou ferramenta); `git grep` do `CLAUDE.local.md` volta vazio
- [x] 7.4 README: sem evidência pública nova (sem número nem link); só citar a política em código na lista do que o projeto faz, se o README já tiver essa lista
