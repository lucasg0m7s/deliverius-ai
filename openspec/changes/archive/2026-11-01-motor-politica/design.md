## Context

As changes 1 e 2 deixaram o domínio de pedidos pronto: `Order` com itens, linha do tempo (`DeliveryEvent`, mesmo
vocabulário do `OrderStatus`), `delivered_at`, `estimated_delivery_at` e valores em `Money`; `Clock` injetável;
`InvariantError` com código estável; três contratos do import-linter. Não existe `policy/`. A política está
descrita em `docs/modelo-dominio.md`, seção 3 (regras, tetos, prazos, limites e a lista `POL-01` a `POL-12`), e o
lugar do motor no grafo em `docs/arquitetura.md` (`check_policy`: negada volta ao agente, no máximo 2 vezes).
Motivação e escopo: ver `proposal.md`. Comportamento: ver `specs/politica/spec.md`.

Quem vai chamar o motor, e por isso molda a assinatura:

- o nó `check_policy` (change 12), com o pedido lido pela ferramenta e o histórico do cliente;
- o `execute_action` (change 11), que **reavalia** dentro da transação, com o pedido travado e o histórico atual
  (`docs/robustez.md`, seção 1: dois reembolsos ao mesmo tempo, aprovação decidida horas depois);
- o gerador de dados (change 6), que usa o teto como gabarito da resolução certa.

## Goals / Non-Goals

**Goals:**

- Uma função pura e determinística: mesmas entradas, mesma decisão; não lê relógio, banco nem arquivo.
- Toda variante da decisão tratada com `match` exaustivo; o mypy acusa o caso esquecido.
- Cada regra da tabela de `modelo-dominio.md` com teste próprio, mais propriedades com Hypothesis.
- Os números num lugar só, e um teste que prova que os documentos não escrevem número à mão.

**Non-Goals:** os itens de "Fora de escopo" da proposal. Em especial, o motor não grava nada, não loga e não sabe da
nota de fraude.

## Decisions

### Estrutura do módulo

```
policy/
  __init__.py     API pública (evaluate, decisões, modelos, POLICY_LIMITS, documentos)
  limits.py       PolicyLimits e POLICY_LIMITS: a fonte única dos números
  models.py       ActionKind, ClaimReason, ClaimedItem, ProposedAction, PriorAction, CustomerHistory,
                  PolicyDocumentId, DenialReason, ApprovalReason, Allowed, NeedsApproval, Denied, PolicyDecision
  engine.py       evaluate e as regras, uma função curta por etapa
  documents.py    PolicyDocument, load_policy_documents, render_policy_document, PolicyDocumentError
  docs/           POL-01-cancelamento.md … POL-12-privacidade.md (dados do pacote)
```

`policy` fica na camada 1: importa `orders` (camada 0) e `shared`, e nada acima. `actions`, `agent` e `datagen`
importam `policy`.

### Entradas: fatos, não repositório

```python
def evaluate(
    action: ProposedAction,
    order: Order,
    history: CustomerHistory,
    claimed_at: datetime,
    limits: PolicyLimits = POLICY_LIMITS,
) -> PolicyDecision: ...
```

```python
class ActionKind(StrEnum): ...  # REFUND, COUPON, CANCELLATION


# Mesmos nomes das intenções, onde existem: ITEM_MISSING, WRONG_ITEM, QUALITY_ISSUE,
# LATE_DELIVERY, NOT_DELIVERED, DUPLICATE_CHARGE, CUSTOMER_CANCELLATION
class ClaimReason(StrEnum): ...


@dataclass(frozen=True, slots=True)
class ClaimedItem:
    order_item_id: OrderItemId
    quantity: int


@dataclass(frozen=True, slots=True)
class ProposedAction:
    kind: ActionKind
    reason: ClaimReason
    amount: Money
    items: tuple[ClaimedItem, ...] = ()


class ApprovedBy(StrEnum): ...  # AUTOMATIC, REVIEWER


@dataclass(frozen=True, slots=True)
class PriorAction:  # uma ação já executada
    order_id: OrderId
    kind: ActionKind
    reason: ClaimReason
    amount: Money
    items: tuple[ClaimedItem, ...]
    executed_at: datetime
    approved_by: ApprovedBy


@dataclass(frozen=True, slots=True)
class CustomerHistory:
    actions: tuple[PriorAction, ...]
```

- **O motor recebe os fatos já carregados**, em vez de uma porta de repositório. Assim a mesma função roda no nó do
  grafo, dentro da trava do `execute_action` (com os dados que a transação travou) e no gerador de dados, sem
  banco. Alternativa: o motor receber `OrderReader` e um leitor de ações, descartada porque o tornaria assíncrono,
  dependente de I/O e incapaz de reavaliar com os dados exatos da transação.
- **O contrato de quem chama:** `history.actions` traz toda ação executada do cliente desde `claimed_at` menos a
  janela de 30 dias **e** toda ação executada no pedido avaliado, de qualquer data. O motor filtra por pedido e por
  janela; a soma dos 30 dias e o "já reembolsado" ficam num lugar só (o motor), e a consulta de quem chama só
  precisa limitar a quantidade.
- **`claimed_at` vem de fora,** nunca do relógio (`convencoes-codigo.md`: função que calcula não lê o relógio). É
  aware; um instante sem fuso levanta `InvariantError`, como no resto do domínio.
- **`ProposedAction` é o modelo do domínio, e não o `ActionProposal`.** O `ActionProposal` é o Pydantic que o LLM
  devolve (change 12) e é convertido para `ProposedAction` na fronteira. `ProposedAction` não tem invariantes além
  das do `Money`: uma proposta mal formada vinda do LLM é esperada, então vira decisão negada, não exceção
  (`convencoes-codigo.md`, "Erros").
- **Itens repetidos na proposta somam as quantidades,** em vez de serem recusados: o resultado é o mesmo de citar o
  item uma vez, e a validação de quantidade cobre o abuso.

### Saída: união de três dataclasses

```python
@dataclass(frozen=True, slots=True)
class Allowed:
    ceiling: Money
    document: PolicyDocumentId


@dataclass(frozen=True, slots=True)
class NeedsApproval:
    ceiling: Money
    document: PolicyDocumentId
    reasons: tuple[ApprovalReason, ...]  # nunca vazio (InvariantError)


@dataclass(frozen=True, slots=True)
class Denied:
    reason: DenialReason
    ceiling: Money | None  # None quando não há teto (não coberto, proposta mal formada)
    document: PolicyDocumentId | None  # None quando a situação não é coberta


type PolicyDecision = Allowed | NeedsApproval | Denied
```

- `PolicyDocumentId` é um `StrEnum` (`POL_03 = "POL-03"`), então um documento inexistente não compila.
- **Códigos de motivo** (`StrEnum`, valores em inglês, estáveis, só se acrescentam):
  - `DenialReason`: `not_covered`, `amount_not_positive`, `items_required`, `items_not_allowed`,
    `item_not_in_order`, `invalid_quantity`, `item_already_claimed`, `order_not_delivered`, `order_in_progress`,
    `order_already_dispatched`, `order_status_not_eligible`, `claim_window_expired`, `delay_below_minimum`,
    `delay_already_compensated`, `exceeds_ceiling`;
  - `ApprovalReason`: `marked_as_delivered`, `duplicate_charge_unverified`, `restaurant_already_accepted`,
    `exceeds_action_limit`, `exceeds_coupon_limit`, `exceeds_customer_limit`.
- **Negada leva o primeiro motivo; precisa de aprovação leva todos.** O agente revisa uma coisa por vez, e a ordem
  das etapas é fixa, então o motivo é previsível. O revisor precisa ver tudo o que levou o caso até ele.
  Alternativa: devolver todas as negativas, descartada porque metade delas é consequência da primeira (item fora do
  pedido também gera teto zero).
- Os três motivos de estado com nome próprio (`order_not_delivered`, `order_in_progress`, `order_already_dispatched`)
  são os que mudam o que o agente deve fazer (esperar, tratar como atraso, oferecer cupom). O resto é
  `order_status_not_eligible`.
- Alternativa à união: exceção para negada (`PolicyDenied`), descartada pela convenção do projeto (o esperado é
  resultado) e porque o `match` exaustivo não funciona com exceção.

### Etapas do `evaluate`, em ordem fixa

1. **Forma:** valor maior que zero; combinação de ação e situação coberta (tabela da spec); itens exigidos ou
   proibidos conforme a situação.
2. **Estado e prazo:** o estado exigido pela situação e a janela, a partir do evento certo (tabela abaixo).
3. **Itens e compensação anterior:** cada item existe no pedido; a quantidade somada fica entre 1 e o que ainda
   não foi reclamado. No atraso, um cupom ou cancelamento por atraso já executado no pedido →
   `delay_already_compensated` (o cupom não reduz o "restante a reembolsar", então sem esta regra o mesmo atraso
   renderia um cupom por proposta).
4. **Teto:** calculado pela situação e limitado pelo que ainda não foi reembolsado no pedido (reembolsos e
   cancelamentos); valor acima → `exceeds_ceiling` com o teto.
5. **Sempre humano:** marcado como entregue, cobrança duplicada, cancelamento depois do aceite.
6. **Limites de autoaprovação:** por ação, de cupom e do cliente em 30 dias, exceto no cancelamento antes do aceite.
7. **Resultado:** sem motivo de aprovação → `Allowed`; com motivo → `NeedsApproval`.

Cada etapa é uma função privada pequena que devolve `Denied | None` (ou a lista de motivos de aprovação); o
`evaluate` só as encadeia. As regras por situação usam `match` sobre `ClaimReason` com `assert_never`.

**De onde vem cada instante:**

| Situação | Instante de partida da janela |
| --- | --- |
| Item faltando, errado, qualidade; atraso depois da entrega | `order.delivered_at` |
| Não entregue com falha | `occurred_at` do evento `delivery_failed` |
| Não entregue marcado como entregue | `order.delivered_at` |
| Cobrança duplicada | `order.placed_at` |

O atraso é `(delivered_at ou claimed_at) − estimated_delivery_at`. Um pedido `delivered` sem `delivered_at`, ou
`delivery_failed` sem o evento, é dado inconsistente: o motor levanta `InvariantError`
(`policy.delivered_without_timestamp`, `policy.failed_without_event`), porque decidir dinheiro sobre ele seria
chute. Alternativa: acrescentar essa invariante ao `Order`, descartada nesta change para não mexer na spec
`pedidos`; fica anotada como evolução.

### `PolicyLimits`: números em código, não em variável de ambiente

```python
@dataclass(frozen=True, slots=True)
class PolicyLimits:
    item_claim_window: timedelta = timedelta(hours=24)
    late_claim_window: timedelta = timedelta(hours=24)
    not_delivered_claim_window: timedelta = timedelta(hours=48)
    duplicate_charge_claim_window: timedelta = timedelta(days=90)
    quality_refund_percent: int = 50
    minor_delay: timedelta = timedelta(minutes=30)
    major_delay: timedelta = timedelta(minutes=60)
    minor_delay_coupon_percent: int = 10
    minor_delay_coupon_cap: Money = Money(1_500)
    major_delay_coupon: Money = Money(2_000)
    auto_approval_limit: Money = Money(5_000)
    coupon_auto_approval_limit: Money = Money(2_000)
    customer_auto_approval_limit: Money = Money(10_000)
    customer_auto_approval_window: timedelta = timedelta(days=30)
    instant_refund_credit_time: timedelta = timedelta(days=1)  # só texto (POL-11)
    card_refund_credit_time: timedelta = timedelta(days=10)  # só texto (POL-11)


POLICY_LIMITS: Final = PolicyLimits()
```

- `__post_init__` confere as invariantes (percentuais entre 0 e 100, `minor_delay < major_delay`, janelas
  positivas). Como `POLICY_LIMITS` é criado na importação, um número inválido derruba a subida com o código da regra.
- **O nome do campo é o nome do marcador** (`{{auto_approval_limit}}`). A formatação sai do tipo: `Money` →
  `R$ 50,00`; `int` (só percentuais) → `50%`; `timedelta` → minutos se não for hora cheia (`30 minutos`),
  horas até menos de 3 dias (`24 horas`, `48 horas`) e dias a partir daí (`90 dias`). Prazo de reclamação se lê
  melhor em horas; "1 dia" no lugar de "24 horas" esconderia que o prazo conta do minuto da entrega.
- Os dois prazos de crédito não entram na decisão; estão aqui porque aparecem no texto e o texto não escreve número
  à mão.
- `evaluate` recebe `limits` com padrão `POLICY_LIMITS`, para os testes variarem um número sem tocar no global.
- Alternativas: variáveis de ambiente (descartada: a política é regra de negócio revisada em PR, não configuração
  de implantação, e o texto indexado precisaria saber qual ambiente leu); YAML (descartado: perde o tipo e o mypy,
  e traz uma dependência).

### Documentos e marcadores

- Os doze arquivos ficam em `src/deliverius/policy/docs/` como dados do pacote, lidos com `importlib.resources`.
  Assim a imagem Docker já os leva para o comando de indexação (change 8). Alternativa: `docs/` na raiz, descartada
  porque não vai para o pacote.
- Formato: `# POL-03 · Item faltando`, depois seções `##` (quando se aplica, o que o Deliverius faz, prazo, quando
  uma pessoa analisa). As seções viram os trechos citáveis da change 8 (`POL-03#prazo`).
- `load_policy_documents()` devolve `tuple[PolicyDocument, ...]` (id, título, markdown com marcadores) e confere:
  exatamente `POL-01` a `POL-12`, o nome do arquivo bate com o título, há pelo menos uma seção `##`.
- `render_policy_document(document, limits)` troca `{{nome}}` pelo valor formatado. Marcador desconhecido →
  `PolicyDocumentError` (subclasse de `DeliveriusError`) com o documento e o marcador. A regex
  (`\{\{\s*([a-z_]+)\s*\}\}`) é linear e só roda sobre arquivos do repositório.
- Teste "nenhum número à mão": sobre o texto **cru**, nenhuma ocorrência de `R$` seguido de dígito nem de número
  seguido de `%`, `minuto`, `hora` ou `dia`.
- Alternativa: Jinja2, descartada (dependência nova e lógica de template que não queremos no texto da política).

### Regras de negócio decididas aqui (confirmadas em 2026-11-02)

A tabela de `modelo-dominio.md` deixa estes pontos em aberto. A spec já está escrita com a recomendação abaixo;
mudar qualquer uma muda só a spec, um teste e uma linha do motor.

| # | Ponto | Recomendação adotada | Alternativa |
| --- | --- | --- | --- |
| R1 | Cancelamento antes do aceite e os limites de valor | **Isento** dos limites por ação e de 30 dias: não há incidente a verificar nem comida feita | Aplicar o limite de R$ 50 (pedidos acima iriam para humano) |
| R2 | Cobrança duplicada "confirmada" | **Sempre humano**: não existe dado de cobrança para confirmar | Permitir com teto, confiando no relato |
| R3 | O que conta nos R$ 100 em 30 dias | **Reembolsos e cupons autoaprovados**, de qualquer pedido; aprovados por humano não contam | Só reembolsos; ou tudo, inclusive o aprovado |
| R4 | O que é "não entregue" | **Falha na entrega** (reembolso) ou **marcado como entregue** (humano); pedido ainda a caminho segue a regra de atraso | Aceitar "não entregue" depois de X minutos de atraso |
| R5 | Base dos 10% do atraso | **Total do pedido** (com taxa e desconto) | Subtotal dos itens |
| R6 | Item reclamado duas vezes por motivos diferentes | **Bloqueado**: unidade que já recebeu reembolso ou cupom conta como reclamada | Permitir um cupom de qualidade e depois o reembolso por falta |
| R7 | Cancelamento depois do aceite ("depende do restaurante") | **Precisa de aprovação** (`restaurant_already_accepted`): o revisor consulta o restaurante e decide no mesmo fluxo | Transferência (`handoff`) sem pedido de aprovação |
| R8 | Teto da situação × limite de autoaprovação | Passar do **teto** → negada (o agente revisa); passar do **limite** → precisa de aprovação | Tudo acima de qualquer número vai para humano |

### Cobertura de ramos e fronteira no CI

- `[tool.coverage.run] branch = true` passa a valer para o projeto todo (o total continua com mínimo de 85%; hoje
  está em 95%).
- Uma etapa nova no `ci.yml` roda só os testes de `policy` exigindo 100% dos ramos do módulo, algo como
  `uv run pytest tests/unit/policy -o addopts="" --cov=deliverius.policy --cov-branch --cov-fail-under=100`. A
  sintaxe exata é conferida na documentação da versão instalada do `pytest-cov` antes de escrever a etapa.
- Contrato novo do import-linter, **"policy is pure"**: `deliverius.policy` não importa `sqlalchemy`, `fastapi`,
  `structlog`, `httpx`, `langgraph`, `langchain`, `langchain_core`, `deliverius.db`, `deliverius.observability`
  nem `deliverius.api`. O contrato de camadas passa a `api → policy → orders`, e o de utilitários ganha
  `deliverius.policy` entre os proibidos.

### Como se testa

- **Uma classe de teste por linha da tabela** de `modelo-dominio.md`, seção 3, e uma por limite, com nomes que
  dizem a regra (`test_missing_item_refund_is_capped_at_item_price`). Os cenários F1, F2, F4a e F7 de
  `fluxos-do-agente.md` viram testes com os mesmos valores.
- **Propriedades (Hypothesis)**, sobre pedidos, propostas e históricos gerados:
  - se não é negada, `amount ≤ ceiling`;
  - se não é negada e é reembolso ou cancelamento, o reembolsado no pedido mais o valor não passa do total;
  - determinismo: duas chamadas iguais dão a mesma decisão;
  - monotonia: se um valor é negado por teto, qualquer valor maior também é;
  - acrescentar ação autoaprovada ao histórico nunca transforma "precisa de aprovação" em "permitida";
  - uma proposta permitida, depois de entrar no histórico como executada, não é permitida de novo para os mesmos
    itens.
- Os pedidos vêm dos construtores de `tests/support/order_builders.py`; o instante, de datas fixas.

### Avaliação

Não há LLM nem modelo nesta change. O motor é o gabarito: a métrica "violações de política" dos evals
(`ml-e-avaliacao.md`, 2ª parte, meta zero) é medida com esta mesma função na change 15.

## Checklist de robustez (`docs/robustez.md`, seção 11)

- **Autorização:** nenhuma rota nem ferramenta nova. O motor recebe um `Order` que quem chama leu com `AccessScope`,
  e só considera do histórico as ações do próprio pedido (por `order_id`) e as do cliente que quem chama passou. A
  matriz de autorização não muda.
- **Corrida:** o motor não lê nem escreve, então não tem corrida própria. Ele é a peça que resolve a corrida de
  "dois reembolsos no mesmo pedido": a change 11 trava o pedido e reavalia com o histórico atual. Aqui o teste
  unitário prova a propriedade que a 11 usa (a segunda proposta, com a primeira no histórico, é negada); o teste
  que dispara as duas juntas fica na change 11, onde há escrita.
- **Idempotência:** não se aplica (função pura e determinística).
- **Timeout e novas tentativas:** não se aplica (nenhuma chamada externa).
- **Limites de recurso:** custo linear nos itens do pedido e nas ações do histórico. O histórico é limitado pela
  janela de 30 dias que quem chama consulta (com `LIMIT`, na change 11); a quantidade reclamada é limitada pela do
  pedido.
- **Queda no meio:** não se aplica (sem estado).
- **Transação com rede ou LLM:** nenhuma. O motor pode rodar dentro da transação da change 11 justamente por ser
  puro e rápido.
- **Eventos de log:** o motor não loga (é puro, como `shared`). Ficam reservados para quem chama: `policy.allowed`,
  `policy.needs_approval` e `policy.denied`, com o tipo da ação, a situação, os códigos de motivo, o documento e os
  valores em centavos, sem dado pessoal. Viram a métrica "negativas da política por motivo" do painel
  (`observabilidade.md`).
- **Entrada não confiável:** a proposta vem do LLM. Mesmo depois da validação do Pydantic (change 12), o motor a
  trata como não confiável: item fora do pedido, valor zero, quantidade zero, negativa ou maior que a pedida viram
  negada. A única regex nova roda sobre os documentos do repositório.
- **Compatibilidade:** módulo novo, nenhuma mudança no banco, no grafo ou na API. Os códigos de motivo e de
  documento serão gravados no estado do grafo e nos pedidos de aprovação (changes 11 e 12), então só se
  acrescentam valores; nenhum é renomeado ou removido.
- **Índices e consultas:** nenhuma consulta nova.
- **Travas e migrations:** nenhuma.
- **Configuração:** nenhuma variável nova. Um número inválido em `PolicyLimits` falha na importação, com o código da
  regra.

## Risks / Trade-offs

- [Os números são provisórios] → ficam só em `limits.py`; os documentos e os testes de exemplo seguem os nomes, e os
  testes de propriedade não dependem dos valores.
- [Uma regra de negócio da tabela R1–R8 for decidida de outro jeito] → cada uma é uma linha do motor e um teste;
  o design aponta qual.
- [Dado inconsistente no pedido (entregue sem horário)] → `InvariantError` em vez de decidir; o gerador de dados
  (change 6) não produz esse caso, e a invariante no `Order` fica como evolução.
- [Quem chama passar um histórico incompleto] → o limite de 30 dias e o "já reclamado" falhariam em silêncio. O
  contrato está na docstring do `CustomerHistory`, e a change 11 tem teste de integração que monta o histórico pela
  consulta real.
- [Arredondamento] → `Money.fraction` arredonda meio centavo para cima, só com inteiros; o teto de 50% de R$ 0,05 é
  R$ 0,03, testado na change 2.
- [O texto dos documentos ficar desatualizado em relação às regras, não só aos números] → os documentos são
  revisados no mesmo PR que muda o motor; o teste garante só os números.

## Migration Plan

Não há migration nem implantação: módulo novo, sem tabela e sem rota. Reverter é remover `policy/` e o contrato
do import-linter.
