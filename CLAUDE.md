# Deliverius AI

Projeto de portfólio do Lucas Gomes Ferreira. O Deliverius é um app de delivery fictício; o **Deli** é o seu
agente de atendimento com IA. O Deli resolve problemas de pedido agindo dentro da política (reembolso, cupom,
cancelamento, escalonamento com aprovação humana), apoiado por modelos próprios: um classificador de intenção ajustado (BERTimbau) e um antifraude de reembolso (regressão logística calibrada, escolhida contra o LightGBM).

Documentação (leia a parte relevante antes de propor ou desenhar uma mudança, em vez de redecidir o que já está
registrado):

| Documento | Conteúdo |
| --- | --- |
| `docs/arquitetura.md` | Estilo (monólito modular), pastas, regras de import, desenho do agente, camada de LLM, RAG |
| `docs/fluxos-do-agente.md` | Os 7 cenários com o estado final esperado (viram testes, evals e demo) |
| `docs/modelo-dominio.md` | Convenções de dados, tabelas, política do Deliverius, gerador de dados sintéticos |
| `docs/ml-e-avaliacao.md` | Ciclo de vida de ML (parte 1) e evals e CI (parte 2) |
| `docs/antifraude.md` | Relatório do antifraude: escolha do modelo, métricas no teste, custo, calibração e limites |
| `docs/convencoes-codigo.md` e `docs/glossario.md` | Padrões de código, testes, dependências e nomes do domínio |
| `docs/seguranca.md` | OWASP LLM e agêntico, API, LGPD, mascaramento, provedores, checklist de revisão |
| `docs/observabilidade.md` | Traces, logs, métricas do painel, alertas |
| `docs/autorizacao.md` | Quem vê e faz o quê: atores, matriz de acesso, escopo no repositório, token, teste da matriz |
| `docs/robustez.md` | Corrida, idempotência, limites, falhas no meio, entrada não confiável, compatibilidade; checklist do design |
| `docs/entrega.md` | Docker, hospedagem, demo, sandbox, pipeline de entrega, README |
| `docs/decisoes-arquitetura.md` | Todas as decisões, com alternativas |
| `docs/requisitos.md` | Requisitos do projeto (R01–R26) → parte do projeto → evidência → changes |
| `docs/roadmap.md` | Changes do OpenSpec nomeadas: escopo, dependências, requisitos e critério de pronto; núcleo e extras |

O Deliverius é inventado: nenhuma marca, cor ou nome de empresa real no projeto.

## Comandos

```bash
uv sync --group research                        # dependências, com o grupo da pesquisa (Python 3.13 pelo uv)
docker compose up -d                            # Postgres 17 + pgvector na porta 5433
uv run alembic upgrade head                     # migrations
uv run fastapi dev src/deliverius/api/main.py   # API em http://localhost:8000 (docs em /docs)
uv run pytest                                   # testes unitários
uv run pytest -m integration --no-cov           # integração (Testcontainers sobe o Postgres; precisa do Docker)
```

**Verificação antes de concluir qualquer tarefa** (os mesmos passos do CI):

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run lint-imports
uv run pytest                                   # unitários, com cobertura mínima de 85%
uv run pytest -m integration --no-cov
```

## Convenções

- **Idioma** (regra completa em `docs/convencoes-codigo.md`, seção "Idioma"):
  - **inglês:** código, comentários, nomes de teste, configuração, logs, exceções e erros da API (`code`, `title`,
    `detail`);
  - **português:** documentação, specs do OpenSpec, commits (Conventional Commits 1.0.0, descrição em português),
    prompts e todo texto que o cliente final lê. A interface traduz o `code` do erro; a API nunca monta texto
    para o cliente.
- **Código limpo e SOLID** (`docs/convencoes-codigo.md`, seção "Código limpo e SOLID"): nomes que dizem a
  intenção, funções curtas com uma responsabilidade, sem código morto nem duplicação de regra, abstração só com um
  segundo caso real. Comentário só para o porquê não óbvio, nunca para repetir o código.
- Tipagem estrita (`mypy --strict` em `src/`). Dados que cruzam módulos são modelos Pydantic ou dataclasses, nunca `dict` solto.
- Configuração só por variável de ambiente, lida em `config.py`. Segredos nunca no repositório; variável nova entra
  em `.env.example`.
- Testes marcados `integration` (banco) e `llm` (chamada real a LLM) ficam fora do `pytest` padrão. Os demais usam
  fakes.
- Ação com efeito (reembolso, cupom, cancelamento) sempre passa pelo motor de política e é idempotente.
- **Ninguém vê o que é de outro** (`docs/autorizacao.md`): rota protegida por padrão, papel por rota, todo
  repositório recebe um `AccessScope` e filtra no `WHERE`, objeto de outro → `404`, modelo de resposta por papel,
  e toda rota nova entra no teste da matriz de autorização.
- **Robustez não é opcional** (`docs/robustez.md`): leitura seguida de escrita é suspeita de corrida e tem trava
  ou restrição, com um teste que dispara as operações juntas; nova tentativa só em operação idempotente; toda
  chamada externa tem timeout; todo recurso tem limite; nada de rede ou LLM dentro de transação; mudança no grafo,
  no banco ou na API é aditiva; cada change registra seus eventos de log. O `design.md` responde ao checklist.
- **Banco eficiente** (`docs/robustez.md`, seção 8): índice para todo filtro e toda chave estrangeira, plano
  conferido com `EXPLAIN`, `LIMIT` e paginação por cursor, `lazy="raise"`, escrita em lote, travas em ordem fixa
  com `lock_timeout`, migration sem trava longa.
- **Prompt injection** (`docs/seguranca.md`, seção 1.3): texto de fora só em bloco delimitado e escapado, nunca
  no prompt de sistema; resposta sem link ou imagem fora da lista permitida; nenhuma decisão depende de o LLM
  obedecer.
- **Disciplina de mudança** (`docs/convencoes-codigo.md`): nunca afrouxar teste, cobertura ou lint para passar;
  bug corrigido vem com teste que falhava; caminho triste testado; API de biblioteca conferida na documentação da
  versão instalada; nada fora do escopo.
- **Commits** (`docs/convencoes-codigo.md`, seção "Commits"): [Conventional Commits 1.0.0](https://www.conventionalcommits.org/pt-br/v1.0.0/),
  `tipo(escopo): descrição` em português, minúscula, no presente (`feat(policy): adiciona teto de reembolso`),
  um assunto por commit, rodapé `Refs: <change>`. O gancho `commit-msg` do pre-commit confere o formato.
- Dados pessoais são mascarados antes de LLM e de log.
- Toda mudança relevante passa pelo OpenSpec (`/opsx:propose` → `/opsx:apply` → `/opsx:archive`).
- O Lucas está aprendendo com o projeto: ao terminar uma fase, explique o que foi feito, por quê, e o que estudar.
- Não commitar nem publicar nada sem o Lucas pedir.
- O repositório é público e genérico: docs, specs, código, commits e README não citam o contexto pessoal do autor.
  Esse contexto fica no `CLAUDE.local.md`, que não vai para o git.
