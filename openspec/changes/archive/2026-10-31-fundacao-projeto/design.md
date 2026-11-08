## Context

Primeira change de código. As escolhas de stack estão em `docs/decisoes-arquitetura.md` e em
`docs/convencoes-codigo.md` (Python 3.13, uv, FastAPI, PostgreSQL + pgvector, SQLAlchemy 2 com asyncpg, Alembic,
porta 5433, Testcontainers, import-linter). Aqui ficam só as decisões de como montar a fundação. Já existe um
rascunho em Python 3.12, escrito antes da arquitetura; ele é revisto para seguir estas decisões.

## Decisions

### Imagem `pgvector/pgvector:pg17`
Imagem oficial do projeto pgvector, com a extensão já compilada, usada no docker compose e no Testcontainers.
Alternativa: compilar sobre `postgres:17`, descartada por não trazer ganho.

### Health check com duas verificações
`GET /health` executa `SELECT 1` e confere se a extensão `vector` está instalada. Responde 200 com
`{"status": "ok", "checks": {"database": "ok", "pgvector": "ok"}}` ou 503 com `status` `degraded` apontando o que
falhou. Sem autenticação: é chamado por orquestradores. Alternativa: só `SELECT 1`, descartada porque o banco pode
estar no ar sem a migration aplicada, e o projeto depende do pgvector. O `/ready`, que também confere os modelos,
fica para a change 9.

### Verificações atrás de um protocolo
O endpoint depende de um `HealthProbe` (protocolo com `check_database` e `check_pgvector`), injetado pelo FastAPI.
A implementação real usa a engine; os testes unitários usam uma falsa. Alternativa: mockar a engine do SQLAlchemy,
descartada por acoplar o teste a detalhes da biblioteca.

### Engine criada no lifespan
A engine nasce no `lifespan` do FastAPI, fica em `app.state` e é descartada no shutdown. Alternativa: engine global
no import, descartada por abrir conexão ao importar o módulo e dificultar testes.

### Testes de integração com Testcontainers
Os testes `integration` sobem um `pgvector/pgvector:pg17` descartável por sessão do pytest e aplicam as migrations
nele. O mesmo comando funciona no local e no CI (o runner Ubuntu já tem Docker). Alternativa anterior: o serviço
Postgres do GitHub Actions, descartada porque o local usaria outro banco (o do docker compose, com dados de
desenvolvimento) e os testes deixariam de ser iguais nos dois lugares.

### import-linter desde a primeira change
Os contratos de `docs/arquitetura.md` entram aos poucos: só se declaram contratos sobre módulos que existem. Nesta
change existem `api`, `db` e `config`, então o contrato inicial garante que `db` e `config` não importam `api`. Cada
change que cria um módulo adiciona o contrato dele. Alternativa: declarar todos os contratos já, descartada porque o
import-linter falha com módulos inexistentes.

### CI em um workflow, com jobs paralelos
`ci.yml` com três jobs: `quality` (ruff, mypy, lint-imports, pytest com cobertura, pytest de integração),
`security` (Gitleaks no histórico e pip-audit nas dependências exportadas do lock) e `image` (build do Dockerfile,
sem publicar). Alternativa: um job só, descartada porque falhas de segurança e de imagem ficam escondidas atrás de
um teste quebrado.

### Dockerfile multiestágio, ainda sem modelos
Primeiro estágio com o uv instala as dependências do lock (sem o grupo `dev`); o segundo copia o ambiente para
`python:3.13-slim`, roda com UID 1000 e tem `HEALTHCHECK` no `/health`. Na subida, `alembic upgrade head` roda antes
da aplicação. Modelos embutidos e Trivy entram na change E4.

### Idioma
Código, configuração, logs e mensagens de erro em inglês; documentação e specs em português
(`docs/convencoes-codigo.md`, seção "Idioma"). O rascunho tem descrições de marcadores do pytest e comentários do
`.env.example` em português; passam para inglês.

### Robustez (checklist de `docs/robustez.md`)
- **Autorização:** `/health` é pública por desenho (lista de `docs/autorizacao.md`, seção 3) e não expõe versão,
  URL nem mensagem de erro do banco, só o estado de cada verificação.
- **Corrida e idempotência:** não se aplica; a rota só lê.
- **Timeout e limites:** a verificação inteira tem tempo limite (`HEALTH_CHECK_TIMEOUT_SECONDS`, 2 s), abaixo dos
  3 s do `HEALTHCHECK` da imagem; a conexão tem timeout próprio. Pool e `statement_timeout` entram na change 2.
- **Falha no meio:** um erro do banco em qualquer das duas consultas vira `DatabaseUnavailableError` e responde
  503, nunca 500. A exceção herda de `Exception` por enquanto e passa a herdar de `DeliveriusError` na change 2.
- **Entrada, compatibilidade, eventos de log:** não se aplica nesta change; o structlog entra na change 2.

## Risks / Trade-offs

- mypy estrito com SQLAlchemy e FastAPI pode exigir ajustes de tipagem → usar os tipos nativos do SQLAlchemy 2.
- Algum pacote sem wheel para o 3.13 → voltar para o 3.12 e registrar em `convencoes-codigo.md`, como já decidido.
- Testcontainers no Windows depende do Docker Desktop no ar → o teste de integração é pulado com mensagem clara se o
  Docker não responder, em vez de falhar com erro obscuro.
- Cobertura mínima de 85% com pouco código pode barrar por detalhe → medir só `src/` e excluir o `__main__`.
