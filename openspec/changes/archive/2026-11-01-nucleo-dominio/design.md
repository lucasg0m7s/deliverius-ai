## Context

A change 1 deixou a API com `/health`, a engine assíncrona criada no `lifespan`, o Alembic com a extensão `vector`
(migration `0001`) e um contrato do import-linter (`db` e `config` não importam `api`). Não existe domínio, sessão,
log estruturado nem tabela. As decisões de fundo já estão registradas: dinheiro, IDs e tempo em
`docs/modelo-dominio.md`, seção 1; o escopo de acesso em `docs/autorizacao.md`, seção 4; os limites e o desempenho
do banco em `docs/robustez.md`, seções 3 e 8; o formato dos logs em `docs/observabilidade.md`, seção 3. Aqui fica
só o como.

## Goals / Non-Goals

**Goals:**

- Tipos básicos do domínio (`Money`, IDs, `Clock`, erros, `AccessScope`) puros e testados por propriedade.
- Tabelas do domínio de pedidos e a auditoria, com o banco recusando dado inconsistente.
- Um repositório de leitura de pedidos em que **é impossível** consultar sem escopo (o mypy acusa).
- Banco com tempo limite em tudo (consulta, trava, transação parada, espera no pool).
- Logs estruturados desde já, sem dado pessoal.

**Non-Goals:** os itens de "Fora de escopo" da proposal. Em especial, nenhuma rota nova, nenhuma escrita de pedido
pela aplicação e nenhuma tabela de conversa, ação ou aprovação.

## Decisions

### Estrutura dos módulos

```
shared/        money.py, ids.py, clock.py, errors.py, access.py (AccessScope), audit.py (AuditEntry)   puro
db/            engine.py, session.py, tables.py (ORM, fonte do Alembic), errors.py, audit.py (gravação)
observability/ logging.py (structlog e redação)
orders/        models.py, ports.py (OrderReader), repository.py (Postgres), in_memory.py, cursor.py
```

A auditoria fica dividida entre `shared/audit.py` (o modelo da entrada e a checagem de dado pessoal, puros) e
`db/audit.py` (a gravação), porque quem grava auditoria são `actions` e `approvals`, módulos da mesma camada que não
podem importar um ao outro. Alternativa: um módulo `audit/` na camada 2, descartada porque `actions` e `approvals`
estão nessa mesma camada.

### `AccessScope` como união de dois tipos

```python
@dataclass(frozen=True, slots=True)
class CustomerScope:
    sandbox_id: SandboxId
    customer_id: CustomerId


@dataclass(frozen=True, slots=True)
class SandboxScope:  # reviewer and internal tasks
    sandbox_id: SandboxId


type AccessScope = CustomerScope | SandboxScope
```

O repositório trata com `match` e `assert_never`, então um terceiro tipo de escopo esquecido quebra o mypy. Um escopo
"tudo" não existe: tarefas que atravessam sandboxes (limpeza, E2) ganham um método próprio e explícito.
Alternativa: um só dataclass com `customer_id: CustomerId | None`, descartada porque `None` é fácil de passar sem
querer e vira "vê tudo da sandbox".

### `Money`

`@dataclass(frozen=True, slots=True, order=True)` com `cents: int`. Invariantes no `__post_init__`: `int` de
verdade (recusa `bool` e `float`, que o Python aceitaria) e não negativo. Operações: `+`, `-` (recusa resultado
negativo), `* int`, `sum` (com `Money.zero()` como início), `fraction(numerator, denominator)` com arredondamento
metade para cima feito só com inteiros (`(cents * num * 2 + den) // (2 * den)`), e `format_brl()` só para
exibição. Só BRL: moeda como campo entra quando houver uma segunda. Alternativa: `Decimal`, descartada por
`modelo-dominio.md` (centavos inteiros são exatos e mais simples de persistir).

### IDs: UUID v7 da biblioteca `uuid-utils`

O Python 3.13 não tem `uuid.uuid7()` (entra no 3.14). `uuid-utils` (licença BSD, mantida, implementação em Rust) tem
o módulo `uuid_utils.compat`, que devolve o `uuid.UUID` da biblioteca padrão, compatível com o tipo `Uuid` do
SQLAlchemy e com o Pydantic. Um `NewType` por entidade (`OrderId`, `CustomerId`, `SandboxId`…) impede trocar um ID
por outro na chamada; `new_id()` é a única função que gera. Ao migrar para o 3.14, só `new_id()` muda.
Alternativas: `uuid6` (Python puro, menos usado) e uma implementação própria da RFC 9562, descartadas por ser código
a manter sem ganho. **A versão da biblioteca é conferida na documentação antes de instalar.**

### `Clock`

`Protocol` com `now() -> datetime`. `SystemClock` devolve `datetime.now(UTC)`; `FixedClock` (para testes e para o
gerador de dados) começa num instante e tem `advance(timedelta)`. Os modelos do domínio recusam `datetime` sem fuso
no `__post_init__`.

### Erros

```
DeliveriusError
├── NotFoundError
├── ConflictError
├── InvalidInputError         (ex.: cursor adulterado)
├── ExternalServiceError
└── DatabaseUnavailableError  (change 1, agora com base)
    └── DatabaseTimeoutError  (statement_timeout, lock_timeout, espera no pool)
```

`db/errors.py` tem um gerenciador de contexto `translate_db_errors()` que converte `DBAPIError`, `OSError` e o
`TimeoutError` do pool do SQLAlchemy nesses erros, registra o evento e nunca engole o `CancelledError`. Os
repositórios o usam; nada fora de `db/` e dos repositórios vê exceção do SQLAlchemy ou do asyncpg.

### Tabelas (migration `0002`)

| Tabela | Colunas principais | Restrições e índices |
| --- | --- | --- |
| `customers` | `id`, `sandbox_id`, `name`, `cpf`, `phone`, `email`, `address`, `created_at` | `UNIQUE (id, sandbox_id)` (alvo da FK composta); `UNIQUE (sandbox_id, cpf)` |
| `restaurants` | `id`, `name`, `category`, `created_at` | Catálogo comum a todas as sandboxes: não tem dado pessoal e não precisa ser copiado por visitante |
| `orders` | `id`, `sandbox_id`, `customer_id`, `restaurant_id`, `status`, `payment_method`, `placed_at`, `estimated_delivery_at`, `delivered_at`, `subtotal_cents`, `delivery_fee_cents`, `discount_cents`, `total_cents`, `notes` | FK **composta** `(customer_id, sandbox_id) → customers (id, sandbox_id)`: um pedido não aponta para cliente de outra sandbox. FK `restaurant_id`. `CHECK` de valores ≥ 0, `total = subtotal + taxa − desconto`, `discount ≤ subtotal + taxa`, `status` e `payment_method` na lista. Índice `(sandbox_id, customer_id, placed_at DESC, id DESC)` para a listagem por cursor do cliente; `(sandbox_id, placed_at DESC, id DESC)` para a da sandbox; `(restaurant_id)` para a FK |
| `order_items` | `id`, `order_id`, `name`, `unit_price_cents`, `quantity`, `allergens text[]` | FK com `ON DELETE CASCADE`; `CHECK` preço > 0 e quantidade > 0; índice `(order_id)` |
| `delivery_events` | `id`, `order_id`, `status`, `actor`, `occurred_at` | FK com `ON DELETE CASCADE`; `CHECK` de `status` e `actor`; índice `(order_id, occurred_at)` |
| `audit_log` | `id`, `sandbox_id`, `occurred_at`, `actor_type`, `actor_id`, `action`, `target_type`, `target_id`, `data jsonb`, `trace_id` (anulável, preenchido na change 14) | Trigger `BEFORE UPDATE OR DELETE` por linha e `BEFORE TRUNCATE` por comando, que levantam erro. `CHECK` de `actor_type` e do formato de `action` (`<módulo>.<evento>`). Índices `(sandbox_id, occurred_at)` e `(target_type, target_id)` |

- **Itens e eventos não têm `sandbox_id`:** só são lidos junto do pedido, depois que a consulta do pedido já aplicou
  o escopo. Nenhum método lê item ou evento por ID solto.
- **`sandbox_id` sem chave estrangeira** por enquanto: a tabela `sandboxes` nasce em E2, que adiciona a FK com
  `NOT VALID` e depois `VALIDATE CONSTRAINT`, sem travar a tabela.
- **Enumerações** (`StrEnum` + `text` com `CHECK`, como decidido); ver "Status, linha do tempo e pagamento".
- **Nas classes ORM, relacionamentos com `lazy="raise"`**: carga preguiçosa no SQLAlchemy assíncrono quebra e
  esconderia N+1. A carga é sempre explícita.
- **Migration escrita à mão** a partir do `--autogenerate` revisado, porque o autogenerate não gera trigger nem
  `CHECK` com expressão. O `env.py` passa a usar os metadados de `db/tables.py` e roda cada migration com
  `SET lock_timeout = '5s'`. O `downgrade` apaga na ordem inversa e é testado (upgrade → downgrade → upgrade).
- Tabelas novas e vazias: não há índice `CONCURRENTLY` nesta change.

### Status, linha do tempo e pagamento

Seguem o ciclo usual de um pedido de delivery, com a linha do tempo como registro das transições:

```
placed → accepted → preparing → ready_for_pickup → out_for_delivery → delivered
   └──────────┴──────────┴──────────────┴──→ cancelled         (só antes de sair para entrega)
                                         out_for_delivery → delivery_failed
```

- **`OrderStatus`:** `placed`, `accepted`, `preparing`, `ready_for_pickup`, `out_for_delivery`, `delivered`,
  `cancelled`, `delivery_failed`. O status do pedido é o último da linha do tempo.
- **`delivery_events.status` usa o mesmo `OrderStatus`:** cada evento é a chegada a um status, com `actor` e
  instante. Um tipo de evento separado, com nomes parecidos mas diferentes (`ready` × `ready_for_pickup`), seria a
  mesma informação em dois vocabulários e um convite a divergirem. Atraso não é evento: é calculado da previsão
  contra o instante real, que é o que a política usa.
- **`EventActor`:** `customer`, `restaurant`, `courier`, `system`.
- **Transições permitidas** ficam numa tabela única em `orders/models.py` (`ALLOWED_TRANSITIONS`), testada contra
  o diagrama. Nesta change ninguém muda status; ela serve ao gerador de dados (change 6), que só produz linhas do
  tempo válidas, e à execução de cancelamento (change 11).
- **`PaymentMethod`:** `pix`, `credit_card`, `debit_card`, `meal_voucher`. Só pagamento pelo app, como na maior
  parte dos pedidos de delivery: todos têm estorno pelo mesmo meio, o que a política de estornos (POL-07, POL-11)
  precisa. Dinheiro na entrega fica fora: o reembolso viraria crédito, um caso a mais sem ganho para o projeto.
- Valores novos entram por migration que troca o `CHECK` (aditiva); nenhum valor é removido.

### Repositório de pedidos

```python
class OrderReader(Protocol):
    async def get_order(self, scope: AccessScope, order_id: OrderId) -> Order | None: ...
    async def list_orders(self, scope: AccessScope, page: PageRequest) -> Page[OrderSummary]: ...
```

- **`get_order`**: uma consulta com o pedido filtrado pelo escopo no `WHERE`; se achou, itens e eventos com
  `selectinload` (duas consultas a mais, número fixo, nunca uma por item). Fora do escopo → `None`, igual a
  inexistente; quem traduz para `404` é a API.
- **`list_orders`**: devolve `OrderSummary` (sem itens, sem observação) com só as colunas da lista, paginação por
  cursor *keyset* em `(placed_at, id)` decrescente, `limit` limitado a 50 e uma linha a mais lida para saber se há
  próxima página (sem `count(*)`).
- **Cursor:** `base64url` de um JSON com `placed_at` e `id`, validado por um modelo Pydantic estrito ao decodificar;
  qualquer falha → `InvalidInputError`. Ele não é assinado: o cursor só diz "a partir de onde", e o escopo continua
  no `WHERE`, então adulterá-lo não revela nada de outro dono.
- **Sessão recebida por parâmetro** (`PostgresOrderRepository(session)`), aberta pelo chamador; o repositório não
  faz `commit`.
- **`InMemoryOrderRepository`** cumpre o mesmo contrato, inclusive o escopo, para os testes do grafo (change 12).
  Uma única suíte de testes de contrato roda contra as duas implementações: a de memória no `pytest` padrão, a do
  Postgres no `-m integration`. É assim que se garante a substituição de Liskov.
- **Dados pessoais no domínio:** `Customer` marca nome, CPF, telefone, e-mail e endereço com `field(repr=False)`,
  então um `repr` ou um log por engano não os mostra. O `OrderSummary` e o `Order` levam só o `customer_id`.

### Sessão e engine

- `db/session.py`: `async_sessionmaker(engine, expire_on_commit=False)`. Uma unidade de trabalho é
  `async with session_factory() as session, session.begin():`, aberta pelo chamador.
- `create_engine` passa a configurar: `pool_size` 10, `max_overflow` 5, `pool_timeout` 5 s, `pool_pre_ping`, e no
  `server_settings` do asyncpg `statement_timeout` (5 s), `lock_timeout` (2 s) e
  `idle_in_transaction_session_timeout` (10 s). Tudo por variável de ambiente, validado na subida (`gt=0`).
- O `DatabaseHealthProbe` continua igual; só herda o novo erro base.

### Logs

`observability/logging.py` com `configure_logging(settings)`, chamado no `lifespan` (e pela CLI depois):

- processadores: `contextvars` (para o `trace_id` da change 14), nível, carimbo ISO em UTC, **redação**, e
  `JSONRenderer` (`LOG_FORMAT=json`, padrão em produção) ou `ConsoleRenderer` (`console`, desenvolvimento);
- os logs da biblioteca padrão (uvicorn, SQLAlchemy) passam pelo mesmo pipeline (`ProcessorFormatter`), para não
  haver duas saídas com formatos diferentes;
- **redação plugável:** uma lista de funções `Redactor` aplicada a cada evento. Esta change traz a que troca por
  `[REDACTED]` o valor de chaves sensíveis conhecidas (`password`, `token`, `authorization`, `secret`, `api_key`,
  `cpf`, `email`, `phone`, `name`, `address`), inclusive aninhadas; a change 4 acrescenta o mascarador de texto livre;
- nomes de evento `<módulo>.<evento>`. Eventos criados aqui: `app.started` (sem segredo; só ambiente e versão),
  `db.query_timeout` e `db.pool_timeout` (`warning`, viram métrica no painel), `db.unavailable` (`error`) e
  `orders.invalid_cursor` (`info`).

### Contratos do import-linter

- `shared` puro: não importa `sqlalchemy`, `fastapi`, `structlog`, `langgraph`, `langchain*`, `httpx`.
- Utilitários (`shared`, `db`, `observability`, `config`) não importam `orders` nem `api`.
- Camadas: `api` → `orders`, e `orders` não importa `api`.

### Configuração nova

`DATABASE_POOL_SIZE`, `DATABASE_MAX_OVERFLOW`, `DATABASE_POOL_TIMEOUT_SECONDS`, `DATABASE_STATEMENT_TIMEOUT_MS`,
`DATABASE_LOCK_TIMEOUT_MS`, `DATABASE_IDLE_IN_TRANSACTION_TIMEOUT_MS`, `LOG_LEVEL`, `LOG_FORMAT`. Os nomes seguem o
padrão plano já usado (`DATABASE_URL`), em vez de subgrupos com `__`, para não quebrar as variáveis existentes.

### Robustez (checklist de `docs/robustez.md`, seção 11)

- **Autorização:** sem rota nova (a matriz nasce na change 13). Toda consulta de pedido recebe `AccessScope`,
  obrigatório e tipado; o teste de contrato cobre dono, outro cliente da mesma sandbox, outra sandbox e escopo da
  sandbox. A FK composta impede, no banco, um pedido ligado a cliente de outra sandbox.
- **Corrida:** não há leitura seguida de escrita nesta change (o repositório só lê; a auditoria só insere). Não se
  aplica; a primeira corrida real é a do reembolso, na change 11.
- **Idempotência:** não há operação repetível com efeito. A auditoria registra o que a operação idempotente da
  change 11 decidir; ela mesma não deduplica.
- **Timeout e novas tentativas:** `statement_timeout`, `lock_timeout`, `idle_in_transaction_session_timeout` e
  `pool_timeout`. O repositório **não** tenta de novo: quem decide é o chamador, que sabe se a operação é idempotente.
- **Limites:** pool de 10 + 5; página de no máximo 50; cursor com tamanho máximo antes de decodificar (256
  caracteres), para não decodificar entrada gigante.
- **Falha no meio:** a auditoria grava na mesma transação da operação (`db/audit.py` recebe a sessão do chamador);
  cancelada, a transação é desfeita. `translate_db_errors()` relança `CancelledError`.
- **Transação com rede ou LLM:** não se aplica; as únicas transações são de leitura e de inserção curta.
- **Eventos de log:** listados em "Logs"; `db.query_timeout` e `db.pool_timeout` viram métrica (painel, change E1).
- **Entrada de fora:** o cursor (decodificação estrita e tamanho máximo). A observação do pedido é texto de fora
  guardado como veio, sem ser interpretado; quem a põe num prompt (change 12) a delimita (`seguranca.md`, seção 1.3).
  Nenhuma regex nova roda sobre texto do cliente.
- **Compatibilidade:** só tabelas novas. Coluna `trace_id` já nasce anulável para a change 14 não precisar de
  migration que trave. A FK de `sandbox_id` entra em E2 com `NOT VALID`.
- **Consultas e índices:** cada consulta tem o índice da tabela acima; um teste de integração roda o `EXPLAIN` das
  duas listagens com `enable_seqscan = off` e confere que o índice composto é usável, e outro conta as consultas do
  `get_order` (três, qualquer que seja o número de itens).
- **Configuração que pode faltar:** todas têm padrão seguro e são validadas na subida; valor inválido (zero,
  negativo) impede a aplicação de subir com mensagem clara.

## Risks / Trade-offs

- **Pooler da Neon e `server_settings`:** o pooler em modo transação pode não aceitar parâmetros de inicialização.
  Mitigação, decidida na change E4: usar a conexão direta (o pool é pequeno) ou `ALTER ROLE … SET
  statement_timeout`. O teste de integração garante o comportamento no Postgres comum.
- **Catálogo de restaurantes comum:** se um visitante pudesse criar restaurantes, um veria o do outro. Hoje nenhuma
  rota cria restaurante; se surgir, ele ganha `sandbox_id`.
- **Redação por nome de chave** pega o erro comum (logar `customer` ou `cpf=`), não texto livre com dado pessoal
  dentro. É a segunda barreira; a primeira é o mascarador da change 4.
- **Enumerações fechadas no `CHECK`:** acrescentar um status exige migration. É o preço de o banco recusar valor
  inválido, e a mudança é aditiva.

## Decisões tomadas na revisão (2026-10-31)

1. **Auditoria × limpeza das sandboxes (E2).** Nesta change o trigger recusa `UPDATE`, `DELETE` e `TRUNCATE` sem
   exceção. Para E2, fica decidido o padrão de mercado para registro imutável com retenção: **uma única porta de
   remoção, estreita e auditável**. Uma função `purge_expired_sandbox_audit(sandbox_id)` com `SECURITY DEFINER`,
   dona de um papel de manutenção, confere que a sandbox expirou e não é a permanente, liga uma marca local da
   transação e apaga; o trigger só aceita `DELETE` com essa marca. Junto, o papel da aplicação perde `UPDATE` e
   `DELETE` sobre `audit_log` (privilégio mínimo, já previsto em `seguranca.md`, ASI03). Alternativas descartadas:
   não apagar a auditoria das sandboxes (cresce sem limite com dado de visitante) e liberar `DELETE` por uma
   variável que a aplicação liga (qualquer bug ligaria).
2. **UUID v7 com `uuid-utils`**, pelo módulo `compat` (devolve `uuid.UUID`): mantida, usada amplamente e trocável
   pelo `uuid.uuid7()` do Python 3.14 numa função só.
3. **Status, linha do tempo e pagamento** como na seção própria acima: o ciclo usual de delivery, um vocabulário
   só para status e eventos, e só pagamento pelo app.
