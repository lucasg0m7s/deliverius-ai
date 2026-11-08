## 1. Projeto Python

- [x] 1.1 `.python-version` e `requires-python = ">=3.13,<3.14"`; `target-version` do Ruff e `python_version` do mypy em 3.13
- [x] 1.2 Dependências principais (fastapi, pydantic-settings, sqlalchemy[asyncio], asyncpg, alembic, pgvector) e grupo `dev` (pytest, pytest-asyncio, pytest-cov, hypothesis, httpx, testcontainers, ruff, mypy, import-linter, pre-commit, pip-audit)
- [x] 1.3 Ruff com as regras de `docs/convencoes-codigo.md` (E, F, I, B, UP, SIM, ASYNC, S, N, RUF, PT, TRY, PERF, C4, FURB, PL sem número mágico e argumentos demais, DTZ, T20); mypy estrito com o plugin do Pydantic; pytest com os marcadores `integration` e `llm` fora do padrão e cobertura mínima de 85% em `src/`
- [x] 1.4 `uv sync` gerando o `uv.lock` com Python 3.13

## 2. Banco local

- [x] 2.1 `docker-compose.yml` com `pgvector/pgvector:pg17` na porta 5433, healthcheck e volume
- [x] 2.2 `.env.example` com `DATABASE_URL` e comentários em inglês; `.gitignore` para `.env`, `.venv`, caches e artefatos de ML
- [x] 2.3 Alembic assíncrono em `migrations/`, lendo a URL de `Settings`; migration inicial `CREATE EXTENSION IF NOT EXISTS vector`

## 3. Código

- [x] 3.1 `deliverius/config.py`: `Settings` e `get_settings()` em cache
- [x] 3.2 `deliverius/db/engine.py`: `create_engine` e `DatabaseHealthProbe`
- [x] 3.3 `deliverius/api/health.py`: protocolo `HealthProbe`, modelo de resposta e rota `GET /health`
- [x] 3.4 `deliverius/api/main.py`: `create_app()` com lifespan (engine em `app.state`)
- [x] 3.5 import-linter: contrato que impede `db` e `config` de importar `api`

## 4. Testes

- [x] 4.1 Unitários do `/health` para os três cenários da spec, com sonda falsa
- [x] 4.2 Integração (`integration`) com Testcontainers: fixture de sessão com `pgvector/pgvector:pg17`, migrations aplicadas, `/health` 200 e ausência da extensão detectada; pula com mensagem clara se o Docker não responder

## 5. Entrega

- [x] 5.1 `Dockerfile` multiestágio com uv, `python:3.13-slim`, UID 1000, `HEALTHCHECK` no `/health`, migrations na subida; `.dockerignore`
- [x] 5.2 `.pre-commit-config.yaml` com Ruff e Gitleaks
- [x] 5.3 `.github/workflows/ci.yml` com os jobs `quality` (ruff format, ruff check, mypy, lint-imports, pytest com cobertura, pytest de integração), `security` (Gitleaks, pip-audit) e `image` (build sem publicar)
- [x] 5.4 README: Python 3.13 e o comando de testes de integração com Testcontainers

## 6. Verificação

- [x] 6.1 `uv run ruff format --check .`, `uv run ruff check .`, `uv run mypy`, `uv run lint-imports`, `uv run pytest`
- [x] 6.2 `uv run pytest -m integration` (Testcontainers)
- [x] 6.3 Import proibido de propósito (`db` importando `api`) quebra o `lint-imports`; desfeito em seguida
- [x] 6.4 Manual: `docker compose up -d`, `uv run alembic upgrade head`, subir a API; `GET /health` com o banco no ar (200) e com o banco parado (503)
- [x] 6.5 `docker build` da imagem; o container sobe contra o banco do compose e responde ao `/health`
