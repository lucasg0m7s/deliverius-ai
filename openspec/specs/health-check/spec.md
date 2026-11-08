# health-check Specification

## Purpose

Permite que um orquestrador de deploy, o CI e quem roda o projeto localmente saibam se a API está no ar e se o
banco, com a extensão pgvector, está pronto.

## Requirements

### Requirement: Endpoint de health check público
A API SHALL expor `GET /health`, acessível sem autenticação, que reporta o estado da aplicação e de cada dependência verificada.

#### Scenario: Tudo saudável
- **WHEN** um cliente faz `GET /health`, o banco responde e a extensão `vector` está instalada
- **THEN** a API responde 200 com `status` igual a `ok` e cada verificação igual a `ok`

### Requirement: Health check reflete o banco e o pgvector
O health check SHALL verificar a conectividade com o PostgreSQL e a presença da extensão `vector`, não apenas que o processo está em execução.

#### Scenario: Banco indisponível
- **WHEN** um cliente faz `GET /health` e a API não consegue conectar ao banco
- **THEN** a API responde 503 com `status` igual a `degraded`, `database` igual a `error` e `pgvector` igual a `skipped`

#### Scenario: Extensão vector ausente
- **WHEN** um cliente faz `GET /health`, o banco responde, mas a extensão `vector` não está instalada
- **THEN** a API responde 503 com `status` igual a `degraded`, `database` igual a `ok` e `pgvector` igual a `missing`

### Requirement: Health check com tempo limitado
O health check SHALL responder dentro de um tempo limite configurável (`HEALTH_CHECK_TIMEOUT_SECONDS`, padrão 2 s), tratando um banco que não responde a tempo como indisponível.

#### Scenario: Banco não responde a tempo
- **WHEN** um cliente faz `GET /health` e o banco não responde dentro do tempo limite
- **THEN** a API responde 503 com `status` igual a `degraded`, `database` igual a `error` e `pgvector` igual a `skipped`

#### Scenario: Banco falha durante a verificação do pgvector
- **WHEN** um cliente faz `GET /health`, o banco responde ao `SELECT 1`, mas falha na consulta da extensão
- **THEN** a API responde 503 com `status` igual a `degraded`, `database` igual a `error` e `pgvector` igual a `skipped`
