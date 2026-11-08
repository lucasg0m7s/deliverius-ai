## 1. Dependências e configuração

- [x] 1.1 Conferir na documentação as versões atuais de `structlog` e `uuid-utils` (e que `uuid_utils.compat.uuid7` devolve `uuid.UUID`); `uv add structlog uuid-utils`; registrar as duas em `docs/arquitetura.md` com o motivo; verificar com `uv sync` e `uv run pip-audit`
- [x] 1.2 `config.py`: variáveis do pool, dos tempos limite do banco e do log, com `Field(gt=0)` e `LOG_FORMAT` como `Literal["json", "console"]`; entram no `.env.example`; teste unitário prova que valor zero ou negativo impede criar o `Settings`

## 2. `shared/` (puro)

- [x] 2.1 `money.py`: `Money` com as invariantes e operações do design; testes de propriedade com Hypothesis (soma associativa e sem perda, subtração nunca negativa, `fraction` dentro de meio centavo do valor exato) e exemplos (50% de 5 centavos = 3; `bool` e `float` recusados); 100% dos ramos
- [x] 2.2 `ids.py`: `NewType` por entidade e `new_id()` com UUID v7; teste confere versão 7 e ordem crescente de IDs gerados em sequência
- [x] 2.3 `clock.py`: `Clock`, `SystemClock` e `FixedClock` com `advance`; teste confere fuso UTC e avanço
- [x] 2.4 `errors.py`: hierarquia do design; `DatabaseUnavailableError` passa a herdar de `DeliveriusError` (a mudança fica em `db/`); testes do `/health` da change 1 continuam verdes
- [x] 2.5 `access.py`: `CustomerScope`, `SandboxScope` e `type AccessScope`
- [x] 2.6 `audit.py`: `AuditEntry` (ator, ação no formato `<módulo>.<evento>`, alvo, dados, instante com fuso, sandbox) que recusa dados com chave de dado pessoal, inclusive aninhada; testes dos dois caminhos

## 3. Banco

- [x] 3.1 `db/engine.py`: pool e `server_settings` (`statement_timeout`, `lock_timeout`, `idle_in_transaction_session_timeout`) vindos do `Settings`; `db/session.py` com `async_sessionmaker(expire_on_commit=False)`
- [x] 3.2 `db/errors.py`: `translate_db_errors()` convertendo erro do driver, `OSError` e o tempo esgotado do pool em `DatabaseUnavailableError`/`DatabaseTimeoutError`, com os eventos `db.query_timeout`, `db.pool_timeout` e `db.unavailable`, relançando `CancelledError`; testes unitários de cada conversão
- [x] 3.3 `db/tables.py`: classes ORM das seis tabelas do design, relacionamentos com `lazy="raise"`, nomes de restrição por convenção (`MetaData(naming_convention=...)`)
- [x] 3.4 `migrations/env.py` usa os metadados de `db/tables.py` e `SET lock_timeout = '5s'`; migration `0002` com tabelas, `CHECK`, FKs (inclusive a composta), índices e os triggers do `audit_log` (`UPDATE`/`DELETE` por linha e `TRUNCATE` por comando); conferir com `uv run alembic upgrade head` no banco do compose
- [x] 3.5 `db/audit.py`: `record(session, entry)` que insere na sessão do chamador, sem `commit`

## 4. `orders/`

- [x] 4.1 `models.py`: `Customer` (dados pessoais com `repr=False`), `Restaurant`, `Order`, `OrderSummary`, `OrderItem`, `DeliveryEvent`, os `StrEnum` (`OrderStatus`, também usado nos eventos, `EventActor`, `PaymentMethod`) e `ALLOWED_TRANSITIONS`; recusam `datetime` sem fuso; testes provam que o `repr` de `Customer` não contém CPF, e-mail nem telefone e que as transições seguem o diagrama do design (cancelar depois de `out_for_delivery` e pular etapa são inválidos)
- [x] 4.2 `cursor.py` e `PageRequest`/`Page`: codificação `base64url`, decodificação estrita com tamanho máximo, `limit` limitado a 50; testes de cursor malformado, adulterado e grande demais → `InvalidInputError`
- [x] 4.3 `ports.py`: `OrderReader`; `repository.py`: `PostgresOrderRepository` com escopo no `WHERE` via `match`/`assert_never`, `selectinload` dos itens e eventos, listagem *keyset* lendo uma linha a mais, erros traduzidos por `translate_db_errors()`
- [x] 4.4 `in_memory.py`: `InMemoryOrderRepository` com o mesmo contrato, inclusive escopo, ordem e paginação
- [x] 4.5 `__init__.py` com a API pública do módulo; `glossario.md` com os termos novos (`AccessScope`, `OrderSummary`, `PaymentMethod`, eventos)

## 5. Logs

- [x] 5.1 `observability/logging.py`: `configure_logging(settings)` com os processadores do design, redação plugável (`Redactor`) e a redação por chave sensível, inclusive aninhada; logs da biblioteca padrão pelo mesmo pipeline; testes com `structlog.testing` provam que `cpf`, `email`, `token` e um `Customer` registrados saem como `[REDACTED]` ou sem os dados
- [x] 5.2 `api/main.py` chama `configure_logging` no `lifespan` e registra `app.started` sem segredo; teste unitário confere o evento e a ausência da URL do banco

## 6. Fronteiras

- [x] 6.1 Contratos do import-linter do design (`shared` puro, utilitários sem `orders`/`api`, `orders` sem `api`); um import proibido de propósito quebra o `lint-imports` e é desfeito

## 7. Testes de contrato e integração

- [x] 7.1 Suíte de contrato do `OrderReader` parametrizada pela implementação: dono lê; outro cliente da mesma sandbox, outra sandbox e ID inexistente → `None`; escopo da sandbox lê qualquer cliente; listagem de 60 pedidos em páginas de 50 + 10 sem repetir nem pular, inclusive com pedido novo inserido entre as páginas; itens e eventos na ordem; roda com a implementação em memória no `pytest` padrão
- [x] 7.2 A mesma suíte contra o Postgres (`-m integration`), com dados inseridos por um ajudante de teste
- [x] 7.3 Integração das restrições: total inconsistente, valor negativo, quantidade zero, status desconhecido e pedido apontando para cliente de outra sandbox são recusados pelo banco
- [x] 7.4 Integração da auditoria: inserção funciona; `UPDATE`, `DELETE` e `TRUNCATE` falham e a linha continua igual; transação desfeita não deixa entrada
- [x] 7.5 Integração dos tempos: `SELECT pg_sleep` acima de um `statement_timeout` baixo → `DatabaseTimeoutError`; pool de 1 conexão ocupada com `pool_timeout` curto → `DatabaseTimeoutError`
- [x] 7.6 Integração de desempenho: `EXPLAIN` das duas listagens com `enable_seqscan = off` usa o índice composto; o `get_order` faz três consultas com 1 e com 20 itens (contadas por evento do SQLAlchemy)
- [x] 7.7 Integração da migration: `upgrade head` → `downgrade 0001` → `upgrade head` sem erro

## 8. Documentação

- [x] 8.1 `docs/decisoes-arquitetura.md`: `AccessScope` como união, `uuid-utils`, catálogo de restaurantes comum, FK composta da sandbox, auditoria dividida entre `shared` e `db`, redação de log por chave, vocabulário único de status e eventos, pagamento só pelo app, e a remoção da auditoria de sandbox expirada por função `SECURITY DEFINER` (para E2), com as alternativas
- [x] 8.2 `docs/modelo-dominio.md`, seção 2: status, eventos (`status` em vez de `type`) e formas de pagamento como no design

## 9. Verificação

- [x] 9.1 `uv run ruff format --check .`, `uv run ruff check .`, `uv run mypy`, `uv run lint-imports`, `uv run pytest` (cobertura ≥ 85% e 100% dos ramos em `shared/money.py`)
- [x] 9.2 `uv run pytest -m integration --no-cov`
- [x] 9.3 Checklists conferidos: `docs/robustez.md` seção 11 (respondido no design) e `docs/seguranca.md` seção 7; `git grep` do `CLAUDE.local.md` volta vazio
- [x] 9.4 README: sem evidência pública nova nesta change; só a lista de comandos, se algum mudar
