## Why

O repositório só tem documentação. Antes de modelar pedidos, gerar dados ou montar o agente, o projeto precisa de
um ambiente Python reproduzível, um banco com pgvector local, uma API mínima e um CI que barre código sem
formatação, sem tipagem, com teste quebrado, com import proibido entre módulos, com segredo ou com dependência
vulnerável. É a change 1 de `docs/roadmap.md` (fase F0) e cobre R01, R11, R18 e R23.

## What Changes

- Projeto Python **3.13** gerenciado pelo uv, pacote `deliverius` em `src/`, com Ruff (formatação e lint, com as
  regras de `docs/convencoes-codigo.md`), mypy estrito com o plugin do Pydantic, pytest, pytest-cov e Hypothesis.
- `docker-compose.yml` com PostgreSQL 17 + pgvector na porta 5433, healthcheck e volume persistente; `.env.example`.
- Configuração tipada (`deliverius.config.Settings`, pydantic-settings) lida de variáveis de ambiente.
- Engine assíncrona do SQLAlchemy 2 com asyncpg; Alembic com a migration inicial que habilita a extensão `vector`.
- App FastAPI com `GET /health`, que verifica o banco e a extensão `vector`.
- Testes de integração com **Testcontainers** (`pgvector/pgvector:pg17`), iguais no local e no CI.
- **import-linter** com os contratos de `docs/arquitetura.md` que já se aplicam aos módulos existentes.
- pre-commit com Ruff e Gitleaks.
- CI no GitHub Actions: ruff, mypy, lint-imports, pytest com cobertura, pytest de integração, **Gitleaks** e
  **pip-audit**, mais o build da imagem.
- `Dockerfile` multiestágio com uv, `python:3.13-slim`, usuário sem root (UID 1000), ainda sem modelos.

Fora de escopo: tabelas do domínio (change 2), structlog e traces (change 14), `/ready` (change 9), modelos
embutidos na imagem, Trivy e deploy (change E4), *problem details* (change 13).

## Capabilities

### New Capabilities
- `health-check`: a API informa se está no ar e se o banco com pgvector responde.

### Modified Capabilities
_Nenhuma: não há specs anteriores._

## Impact

- Cria ou revisa `src/deliverius/`, `tests/`, `migrations/`, `alembic.ini`, `pyproject.toml`, `.python-version`,
  `docker-compose.yml`, `Dockerfile`, `.dockerignore`, `.pre-commit-config.yaml`, `.github/workflows/ci.yml`,
  `.env.example`, `.gitignore`, `README.md`.
- O código rascunho já existente (Python 3.12) é revisto, não descartado.
- Sem decisão humana pendente: as escolhas estão em `docs/decisoes-arquitetura.md`.
