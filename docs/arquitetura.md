# Arquitetura

O Deliverius é um app de delivery fictício, e o **Deli** é o seu agente de atendimento. O Deli resolve problemas
de pedido (atraso, item faltando, item errado, não entregue, cobrança, cupom) agindo dentro da política da empresa:
reembolsa, emite cupom, cancela ou transfere para um humano.

Este documento foi escrito tema por tema. Estado:

| Tema | Estado |
| --- | --- |
| 1. Estilo arquitetural e estrutura de pastas | **Decidido em 2026-10-31** |
| 2. Desenho do agente | **Decidido em 2026-10-31**. Os roteiros dos cenários estão em `fluxos-do-agente.md` |
| 3. Camada de LLM | **Decidido em 2026-10-31** |
| 4. Domínio, dados e RAG | **Decidido em 2026-10-31**. Domínio, política e dados em `modelo-dominio.md`; RAG abaixo |
| 5. Ciclo de vida de ML | **Decidido em 2026-10-31**, em `ml-e-avaliacao.md` (parte 1) |
| 6. Python moderno e convenções | **Decidido em 2026-10-31**, em `convencoes-codigo.md` e `glossario.md` |
| 7. Qualidade (CI e evals) | **Decidido em 2026-10-31**, em `ml-e-avaliacao.md` (parte 2) |
| 8. Segurança e LGPD | **Decidido em 2026-10-31**, em `seguranca.md` |
| 9. Observabilidade e custo | **Decidido em 2026-10-31**, em `observabilidade.md` |
| 10. Entrega | **Decidido em 2026-10-31**, em `entrega.md` |

As decisões resumidas, com alternativas, estão em `decisoes-arquitetura.md`.

## Estilo: monólito modular com portas leves

O Deli é **um único pacote Python** (`deliverius`), implantado como **um único processo**, dividido em **módulos
por capacidade** (pedidos, política, antifraude, agente…). Isso é diferente de dividir por camada técnica
(`routers/`, `services/`, `repositories/`). Três regras:

1. **Regra de negócio sem framework.** `policy`, `privacy` e `shared` são Python puro: não importam FastAPI,
   SQLAlchemy, LangGraph nem SDK de LLM. Isso deixa os testes rápidos e garante que a parte mais crítica (quanto se
   pode reembolsar) não dependa de biblioteca nenhuma.
2. **Frameworks nas bordas.** O FastAPI fica só em `api/` e o LangGraph só em `agent/`. Os SDKs de LLM ficam só em
   `llm/` e o SQLAlchemy só nos repositórios e em `db/`. Trocar ou atualizar um deles mexe num lugar só.
3. **Portas só onde há troca real.** Uma "porta" é um `typing.Protocol`, um contrato que diz o que se espera sem
   dizer como. Ela existe quando há mais de uma implementação de verdade ou quando o teste precisa de um falso:
   - modelo de chat: a porta é o `BaseChatModel` do LangChain (tema 3), com Gemini, compatível com OpenAI (Groq,
     Ollama), Anthropic e um falso para os testes;
   - `IntentClassifier` e `FraudScorer`: modelo real ou falso;
   - os repositórios usados pelas ferramentas do agente: Postgres ou memória.

   Não se cria interface "por precaução".

**Regras de dependência verificadas no CI** com o [import-linter](https://import-linter.readthedocs.io/en/latest/contract_types/)
(seção "Regras de import"). Escrever a regra no documento não basta: um import errado tem de quebrar o build.

### Por que não as outras opções

| Opção | Por que não |
| --- | --- |
| **Camadas técnicas globais** (`routers/`, `services/`, `repositories/`, `models/`), o modelo de muitos templates de FastAPI | Mistura assuntos diferentes na mesma pasta. O classificador, o antifraude e o agente não cabem em "service" nem em "repository". Fica difícil mostrar e testar as fronteiras |
| **Clean Architecture ou hexagonal completa** (entities, use cases, interfaces e adapters para tudo) | É o mesmo raciocínio do Fuja: muita cerimônia para um domínio pequeno, e o prazo é de semanas. Ficamos com a parte que se paga: domínio puro e frameworks nas bordas |
| **Microsserviços** (servir os modelos à parte, agente à parte) | Infraestrutura, rede e deploy multiplicados, sem ganho para um portfólio de um processo. O monólito modular deixa a extração possível depois, porque as fronteiras já existem |

### Decisões que vêm junto

- **Injeção de dependência sem framework.** Um único lugar monta tudo, a *composition root* (`bootstrap.py`):
  engine do banco, provedores de LLM, modelos de ML e grafo. A API chama esse lugar no `lifespan` do FastAPI; a CLI
  e os evals chamam o mesmo.
  - Os nós do LangGraph recebem as dependências pelo **contexto de execução** (`context_schema` e
    `Runtime[Context]`), que é o mecanismo oficial ([docs](https://docs.langchain.com/oss/python/langgraph/graph-api)),
    nunca por variável global.
  - Nos testes, a mesma montagem recebe os falsos.
- **Assíncrono para entrada e saída, thread para CPU.** API, banco (asyncpg) e LLM são `async`. A inferência do
  classificador é cálculo de CPU e roda em thread (`asyncio.to_thread`), para não travar o event loop. O antifraude
  escolhido (regressão logística) são 27 multiplicações: roda direto, sem thread.
- **A aplicação não depende do PyTorch.**
  - O classificador ajustado é exportado para **ONNX** e roda com `onnxruntime`.
  - O antifraude é uma regressão logística aplicada em numpy, a partir de números em JSON (coeficientes,
    padronização e os dois parâmetros do calibrador). O LightGBM foi o candidato e perdeu na regra de escolha
    (`antifraude.md`).
  - scikit-learn, LightGBM, MLflow e matplotlib ficam no grupo de dependências `research`; torch, transformers,
    datasets, accelerate e onnx ficam no grupo `finetune`, fora até do CI (o ajuste fino roda no Kaggle). A
    aplicação não instala nenhum dos dois, e um contrato do import-linter proíbe importá-los em `src/`, assim como
    importar o pacote `research`.

  Resultado: imagem Docker leve, início rápido e CPU gratuita do Hugging Face Spaces suficiente.
- **A aplicação carrega os modelos como artefatos versionados:** do Hugging Face Hub com revisão fixa no código,
  baixados por um comando explícito para uma pasta local e conferidos por tamanho e SHA-256 (`deliverius.artifacts`)
  antes de qualquer leitura. O MLflow é a ferramenta da pesquisa (experimentos e registro), não uma dependência da aplicação em
  execução.
- **As features do antifraude têm um único código** (`fraud/features.py`), usado pelo treino em `research/` e pela
  aplicação. Isso evita o *training-serving skew*, que é o modelo ver em produção features calculadas de um jeito
  diferente do treino.
- **Dependências de base do domínio** (change `nucleo-dominio`):
  - **`structlog`** (26.x): logs estruturados, em JSON em produção e legíveis no desenvolvimento, com um pipeline
    de processadores onde entra a redação de dados pessoais. Os logs da biblioteca padrão passam pelo mesmo
    pipeline, então há um formato só.
  - **`uuid-utils`** (1.x): gera UUID v7, que o Python 3.13 não tem na biblioteca padrão (entra no 3.14). Usamos
    o módulo `uuid_utils.compat`, que devolve o `uuid.UUID` padrão, aceito pelo SQLAlchemy e pelo Pydantic. Só a
    função `shared.ids.new_id()` a chama, então trocar pelo `uuid.uuid7()` do 3.14 é mudar uma linha.

## Estrutura de pastas

```
deliverius-ai/
├── src/deliverius/
│   ├── config.py              Configuração tipada por variável de ambiente (pydantic-settings)
│   ├── bootstrap.py           Composition root: monta e liga as dependências
│   │
│   ├── shared/                Tipos básicos (Money, IDs), relógio, erros base. Puro
│   ├── db/                    Engine assíncrona, sessão, tabelas ORM (fonte do Alembic)
│   ├── artifacts/             Arquivos de um repositório do Hugging Face numa revisão fixa, conferidos por SHA-256
│   ├── observability/         Logs (structlog e redação), OpenTelemetry e helpers de span
│   │
│   ├── orders/                Pedido, item, entrega, cliente, histórico; repositório
│   ├── policy/                Políticas em markdown + motor de política. Puro
│   ├── privacy/               Mascaramento de dados pessoais. Puro
│   │
│   ├── actions/               Execução idempotente de reembolso, cupom e cancelamento; auditoria
│   ├── approvals/             Pedidos de aprovação humana: criar, listar, decidir
│   ├── intent/                Porta IntentClassifier + adaptador ONNX + rótulos
│   ├── fraud/                 Features no ponto do tempo (compartilhadas com o treino), FraudScorer + adaptador
│   │                          logístico, leitor do histórico em Postgres, carga conferida do modelo
│   ├── retrieval/             Indexação e busca híbrida das políticas (pgvector + full-text pt_unaccent + RRF; e5 em ONNX)
│   ├── llm/                   Papéis, fábrica de modelos, preço, uso, orçamento, prompts, cache (tema 3)
│   │
│   ├── agent/                 Grafo LangGraph: estado, nós, ferramentas, prompts versionados, contexto
│   │
│   ├── api/                   FastAPI: conversas (SSE), aprovações, métricas, /health
│   ├── cli/                   Comandos: seed, gerar dados, indexar políticas, rodar evals
│   ├── datagen/               Gerador de cenários e conversas sintéticas
│   └── evals/                 Harness de avaliação (cliente simulado, métricas, relatório)
│
├── research/                  Pesquisa e treino (scripts com YAML, `python -m research.<área>.<etapa>`). Importa `deliverius`; nunca o contrário
│   ├── dataset.py, hub.py     Comuns: a fixação do dataset (`dataset.yaml`) e o envio ao Hub com ensaio, tag e retomada
│   ├── intent/                config.yaml, dados mascarados, as quatro abordagens, métricas, regra de escolha, avaliação
│   │                          final, exportação ONNX, publicação, o prompt do baseline e o notebook do Kaggle
│   └── fraud/                 config.yaml, dataset, split, train, evaluate, candidate, publish e gate (porta do CI)
├── data/                      Só amostras pequenas e README; os datasets completos ficam no Hugging Face com versão
├── tests/
│   ├── unit/                  Espelha src/deliverius/<módulo>/
│   ├── integration/           Marcados `integration` (Postgres no ar)
│   └── llm/                   Marcados `llm` (chamada real a LLM)
├── migrations/                Alembic
├── docs/                      Arquitetura, decisões, convenções, segurança, ML e avaliação, roadmap
└── openspec/                  Specs e changes (SDD)
```

**Dentro de cada módulo**, os mesmos nomes de arquivo, criados só quando há conteúdo:

| Arquivo | O que tem |
| --- | --- |
| `__init__.py` | A API pública do módulo. Os outros módulos importam daqui (`from deliverius.orders import Order`), não dos arquivos internos |
| `models.py` | Modelos do domínio do módulo |
| `ports.py` | Os `Protocol`, quando o módulo tem porta |
| `service.py` | Regras e orquestração do módulo |
| `repository.py` | Acesso ao banco (SQLAlchemy), implementando a porta |
| `<tecnologia>_<coisa>.py` | Adaptadores, como `onnx_classifier.py`, `gemini.py` e `fake.py` |

`datagen/` e `evals/` ficam dentro do pacote porque precisam importar o domínio. Mesmo assim, nenhum módulo da
aplicação os importa: regra de import 4.

## Regras de import

Verificadas pelo import-linter no CI. Uma camada só importa as que estão abaixo dela. Módulos na mesma linha não
importam uns aos outros.

```
5  api │ cli │ evals │ datagen          entradas
4  bootstrap                            montagem
3  agent                                orquestração
2  actions │ approvals │ intent │ fraud │ retrieval │ llm     capacidades
1  policy │ privacy                     regras puras
0  orders                               domínio de delivery
—  shared │ db │ observability │ config │ artifacts   utilitários (qualquer camada pode usar)
```

Contratos, nesta ordem de importância:

1. **Camadas**, como no diagrama acima.
2. **Pureza:** `policy`, `privacy` e `shared` não importam `sqlalchemy`, `fastapi`, `langgraph`, `langchain*`,
   `httpx` nem SDKs de LLM. `policy` e `privacy` também não importam um ao outro nem o `structlog`. O logger recebe o
   `privacy` como função, e quem liga os dois é `api/main.py`, enquanto o `bootstrap` não existe.
3. **Um dono por tecnologia:**
   - integrações de provedor (`langchain_google_genai`, `langchain_openai`, `langchain_anthropic`) e SDKs de
     LLM só em `llm/`;
   - `langgraph` e `langchain` (o `create_agent`) só em `agent/` e `bootstrap`;
   - `langchain_core` só em `llm/`, `agent/` e `bootstrap`;
   - `fastapi` só em `api/`.
4. **Ferramentas de desenvolvimento isoladas:** nenhum módulo das camadas 0 a 4 importa `evals`, `datagen` ou `cli`.

Ainda vale, do rascunho anterior: o agente acessa dados **só pelas ferramentas**. Texto vindo do banco (observação
do pedido, nome de item) entra no prompt como dado delimitado, nunca como instrução. Dados pessoais são mascarados
antes de qualquer LLM e de qualquer log.

## Desenho do agente

### Princípio: fluxo fixo por fora, agente só onde precisa decidir

A Anthropic separa dois tipos de sistema ([Building effective agents](https://www.anthropic.com/research/building-effective-agents)):

- **workflows**, em que o código define o caminho;
- **agentes**, em que o LLM escolhe os passos.

A recomendação é usar a solução mais simples e só aumentar a autonomia quando ela se paga. No Deli:

- **É código, e não LLM:** mascarar, classificar, rotear, aplicar a política, pontuar fraude, pedir aprovação,
  executar e validar a resposta.
- **É agente (LLM com ferramentas):** só a investigação de um problema com pedido, que termina numa **proposta**
  de ação.

### Regra de segurança central: o LLM não tem ferramenta que escreve

As ferramentas do agente **só leem**. Reembolsar, dar cupom e cancelar **não são ferramentas do LLM**. O LLM
devolve uma `ActionProposal` (saída estruturada). Quem executa é um nó de código, depois da política, do
antifraude e, se preciso, do humano.

Consequência: um prompt injection bem-sucedido consegue no máximo produzir uma *proposta* ruim, que os passos
seguintes filtram. Isso responde à "agência excessiva" (LLM06) do OWASP e às listas agênticas.

### O grafo (uma execução por mensagem do cliente)

```
START
  │
  ▼
mask_input ──► classify_intent ──► (roteamento)
                                     │
      ┌──────────────┬───────────────┼───────────────────┬──────────────────┐
      ▼              ▼               ▼                   ▼                  ▼
 handoff_to_human  answer_faq   ask_clarification   support_agent      (fora de escopo
 (sensível)        (RAG+citação) (confiança baixa)   (create_agent,     → answer_faq)
      │              │               │               só leitura)
      │              │               │                   │ ActionProposal
      │              │               │                   ▼
      │              │               │              check_policy ──negada (≤2x)──► support_agent
      │              │               │                   │  negada de novo ──► handoff_to_human
      │              │               │                   │  sem ação ──► compose_reply
      │              │               │                   ▼  permitida
      │              │               │              score_fraud
      │              │               │                   │ precisa de humano?
      │              │               │          sim ┌────┴────┐ não
      │              │               │              ▼         │
      │              │               │      request_approval  │   (cria o pedido, avisa "em análise")
      │              │               │              ▼         │
      │              │               │      await_approval    │   (SÓ o interrupt; pausa aqui)
      │              │               │       aprovado │ recusado ─► compose_reply
      │              │               │              ▼         ▼
      │              │               │           execute_action    (idempotente, auditada)
      │              │               │                   │
      └──────────────┴───────────────┴──────────► compose_reply ──► validate_reply ──► END
```

| Nó | Tipo | O que faz |
| --- | --- | --- |
| `mask_input` | Código (`privacy`) | Troca CPF, cartão, telefone, e-mail e endereço por marcadores. Daqui em diante, estado, LLM e logs só veem o texto mascarado |
| `classify_intent` | Modelo próprio (`intent`) + regra | Intenção e confiança. Uma **rede de segurança por palavras-chave** (alergia, passei mal, acidente, assédio) força o caminho sensível mesmo se o classificador errar. O caminho sensível **não chama nenhum LLM externo** (dado de saúde, `seguranca.md` 3.2) |
| roteamento | Aresta condicional | Sensível → humano; dúvida → FAQ; problema com pedido → agente; confiança abaixo do limiar → pergunta |
| `support_agent` | **Agente** (`create_agent` do LangChain, como subgrafo) | Investiga com ferramentas de leitura e devolve uma `ActionProposal` |
| `check_policy` | Código puro (`policy`) | Decide entre permitida, precisa de aprovação e negada, com motivo e teto. Negada volta ao agente com o motivo, no máximo 2 vezes |
| `score_fraud` | Modelo próprio (`fraud`) | Nota calibrada e os 3 fatores que mais pesaram. Probabilidade × valor pedido ≥ custo da revisão **ou** a política já pediu aprovação → humano. Falha ao pontuar → humano (*fail closed*) |
| `request_approval` | Código (`approvals`) | Cria o pedido de aprovação (idempotente) e gera a mensagem "em análise" para o cliente |
| `await_approval` | `interrupt(..., response_schema=ApprovalDecision)` | **Só** a pausa. Ao retomar, este nó roda de novo; como não tem efeito colateral, isso é seguro |
| `execute_action` | Código (`actions`) | Executa com chave de idempotência e grava a auditoria, na mesma transação |
| `handoff_to_human` | Código (`approvals`) | Transferência com **resumo estruturado** (intenção, fatos, ações já feitas) mais a conversa. O atendente não pergunta de novo |
| `compose_reply` | LLM (ou modelo de texto fixo) | Escreve a resposta a partir dos **fatos** do estado (resultado da execução, citações) |
| `validate_reply` | Código | Confere que todo valor e toda ação citados na resposta existem no resultado executado. Se falhar, gera de novo uma vez; se falhar de novo, usa o texto fixo |

### Ferramentas do agente (só leitura)

| Ferramenta | Devolve |
| --- | --- |
| `get_order` | Itens, valores, status, restaurante, observações (como dado delimitado) |
| `get_delivery_timeline` | Eventos da entrega com horários |
| `get_customer_summary` | Histórico resumido: pedidos, reembolsos recentes, contestações |
| `search_policy` | Trechos das políticas com identificador, para citação |

- **O escopo vem do contexto, não do LLM.** O `customer_id` e o `order_id` vêm do contexto de execução (sessão
  autenticada), e não de argumentos que o LLM escolhe. Assim o agente não consegue consultar o pedido de outro
  cliente, nem se for induzido a isso.
- **Saída delimitada.** Todo texto vindo do banco volta marcado como dado. O prompt diz que conteúdo dentro dessas
  marcas nunca é instrução. Isso cobre o prompt injection indireto, como uma observação maliciosa no pedido.

### Estado e persistência

- **Uma conversa é uma *thread* do LangGraph.** O `thread_id` é o ID da conversa. Cada mensagem do cliente é uma
  execução do grafo.
- **O estado é um `TypedDict`**, com modelos Pydantic dentro (`ActionProposal`, `PolicyDecision`, `FraudScore`,
  `ApprovalDecision`, `ExecutionResult`), mais contadores (chamadas de ferramenta, revisões, turnos) e métricas do
  turno (tokens, custo, latência). As mensagens guardadas já estão mascaradas.
- **O checkpointer é o `AsyncPostgresSaver`, no mesmo Postgres**, com durabilidade `sync`. É uma conversa que mexe
  com dinheiro, e o custo de gravar a cada passo é pequeno num grafo deste tamanho.
- **O pedido de aprovação também vai para a tabela `approval_requests`.** O console consulta essa tabela, nunca os
  checkpoints, que são detalhe interno do LangGraph.
- **`approvals` e `actions` não se importam (mesma camada); o agente liga os dois.** O nó `execute_action` lê a
  aprovação por `approvals` e passa a autoridade para `actions`: `Automatic()` quando a política permitiu, ou
  `ReviewerApproved(approval_request_id, reviewer_id)` depois de uma aprovação. Ler e depois executar em duas
  transações é seguro porque a aprovação decidida é imutável (trigger), e o banco ainda confere o par: a FK
  composta da ação só aceita uma aprovação aprovada, da mesma conversa e da mesma proposta. O executor reavalia a
  política com o `claimed_at` original guardado na aprovação; `Denied` nunca executa.
- **Mensagem nova com aprovação pendente não retoma o grafo.** A API vê a pendência e responde "seu pedido está em
  análise, prazo X", sem executar nada.
- **Retomada:** o console envia a decisão. A API confere quem decidiu, se o pedido ainda está pendente e se a
  versão bate, e então chama o grafo com `Command(resume=decisão)` no mesmo `thread_id`.

### Limites (todos configuráveis; ao estourar → transferência para humano)

| Limite | Valor inicial |
| --- | --- |
| Chamadas de ferramenta por turno | 8 |
| Revisões de proposta após negativa da política | 2 |
| Turnos por conversa | 15 |
| Orçamento diário de LLM | Valor a definir no tema 3. Estourou → modo degradado: só FAQ e transferência |

### Alternativas consideradas

| Opção | Por que não |
| --- | --- |
| **Um agente só (ReAct) com todas as ferramentas, inclusive as que escrevem** | É o desenho mais comum nos tutoriais. A segurança dependeria do LLM obedecer, e o custo cresce, porque todo turno passa pelo LLM, até o "obrigado" |
| **`HumanInTheLoopMiddleware` do LangChain** (pausa antes de uma ferramenta) | Serve quando a ferramenta perigosa está com o LLM. Aqui a decisão de pedir humano depende da política e do antifraude, que são código, então o `interrupt` próprio num nó dedicado é mais claro |
| **Multiagente** (supervisor e especialistas) | O roteamento por classificador já separa FAQ e problema com pedido. Mais agentes significa mais chamadas de LLM e mais pontos de falha, sem ganho no escopo atual. Fica como evolução |
| **Loop de agente escrito à mão** em vez do `create_agent` | O `create_agent` já é um subgrafo LangGraph com saída estruturada e middlewares de limite. Escrever à mão seria repetir isso |

**Efeito no tema 3:** usar o `create_agent` significa usar as integrações de modelo do LangChain. O tema 3
(abaixo) trocou o "contrato próprio de LLM" pelo `BaseChatModel`.

## Camada de LLM

### Contrato: o `BaseChatModel` do LangChain, sem contrato próprio por cima

O rascunho previa um contrato próprio com adaptadores. Com o `create_agent` (tema 2), o agente já fala com o
`BaseChatModel` do `langchain-core`. Esse contrato já é independente de provedor e tem implementações mantidas para
Gemini, para qualquer API compatível com a OpenAI e para Anthropic. Criar outro por cima seria repetir a camada.

O que fica **nosso** em `llm/` é o que nenhuma biblioteca sabe sobre o nosso negócio: papéis, preço, orçamento,
registro de uso, prompts versionados e cache dos evals.

```
llm/
├── roles.py        Papéis e a configuração fixa de cada um (timeout, prazo total, teto de saída, temperatura)
├── factory.py      Cria ChatGoogleGenerativeAI, ChatOpenAI(base_url=…) ou ChatAnthropic a partir de um ModelSpec
├── runtime.py      LlmRuntime e RoleModels: um modelo e um governor por papel e provedor; tentativas e reserva
├── governor.py     O callback de orçamento e de registro de uso (reserva antes da chamada, linha depois)
├── gate.py         ProviderGate: a pausa de um provedor depois de um 429
├── call.py         call_or_degrade: devolve LlmReply ou DegradedMode, nunca uma exceção de provedor
├── pricing.py      Tabela de preços por modelo, com data da consulta, e o custo equivalente em micros de dólar
├── ports.py        LlmUsageStore, ProviderPause e os tipos (BudgetLimit, LlmCallRecord)
├── repository.py   PostgresLlmUsageStore: reserva atômica, soma de tokens e a linha de llm_calls
├── in_memory.py    O mesmo contrato em memória, para os testes
├── errors.py       LlmBudgetExceededError, ProviderPausedError, LlmConfigurationError e PromptError
├── prompts.py      Carrega prompts versionados e devolve texto, versão e hash
├── cache.py        SqliteResponseCache, para evals e geração de dados (nunca em produção)
└── fake.py         ScriptedChatModel: modelo falso com roteiro (inclusive ferramentas) para os testes
```

A API pequena que os outros módulos usam: `LlmRuntime.build(settings, session_factory, clock)`,
`runtime.models_for(papel)` (devolve o principal e a reserva), `RoleModels.runnable()`,
`call_or_degrade(...)`, `call_structured(...)` (devolve o objeto validado contra um esquema Pydantic ou o modo
degradado, com `INVALID_OUTPUT` para saída que não valida), `runtime.budget_status(papel)` (uso do dia, só leitura),
`load_prompt(...)` e `call_metadata(...)`. A montagem falha, citando a variável, se
faltar a chave de um provedor em uso; a API sobe sem LLM, e o `bootstrap` chama a montagem na subida.

### Papéis e provedores

Cada chamada de LLM tem um **papel**. Trocar o modelo de um papel é mudar uma variável de ambiente.

| Papel | Uso | Padrão | Reserva |
| --- | --- | --- | --- |
| `agent` | `support_agent` (ferramentas e proposta estruturada) | Gemini Flash-Lite (geração estável mais recente) | Groq, modelo aberto com tool calling |
| `writer` | `compose_reply`, `answer_faq`, `ask_clarification` | Gemini Flash-Lite | Groq; se tudo falhar, texto fixo |
| `customer_sim` | Cliente simulado nos evals | Modelo de **família diferente** do agente | — |
| `judge` | Juiz de qualidade nos evals | O candidato com **maior kappa** na calibração contra rótulos humanos, de família diferente do agente | — |
| `datagen` | Mensagens sintéticas | Gemini Flash-Lite | — |
| `datagen_holdout` | Teste sintético de outro gerador (`modelo-dominio.md`, seção 4) | Groq `gpt-oss-120b`: família diferente do `datagen` | — |
| `intent_baseline` | Só a pesquisa: o "só perguntar ao LLM" da `pesquisa-intencao` (temperatura 0, 10 s por tentativa, 25 s no total, 128 tokens) | Gemini Flash-Lite | — de propósito: uma resposta de outro modelo no meio misturaria dois modelos na mesma medida |

**Provedores:**

- **Gemini** (`langchain-google-genai`): cota gratuita, padrão de produção.
  - O modelo `gemini-3.5-flash-lite` foi confirmado na página do Google em 2026-11-02 (estável, com ferramentas e
    saída estruturada). Ele fica numa variável de ambiente. A cota gratuita **não é publicada em números**: só o AI
    Studio mostra o RPM e o RPD da conta. Conferido no AI Studio em 2026-11-08, no nível gratuito: 15 RPM, 250.000
    tokens de entrada por minuto e 500 RPD, por projeto, com o dia virando à meia-noite do Pacífico. Os padrões do
    projeto ficam em 80% disso (12 por minuto e 400 por dia, a mesma margem do Groq), e os 2.000.000 de tokens por
    dia são um teto nosso, porque o Google não publica um
    ([modelo](https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite),
    [limites](https://aistudio.google.com/docs/rate-limits)).
  - **500 por dia é pouco para os evals:** 30 cenários × 3 execuções, com várias chamadas por conversa, passam disso
    num dia. O simulador de cliente e o juiz já ficam no Groq, e o cache de evals evita repetir chamada; a change dos
    evals precisa planejar a rodada em mais de um dia ou usar o Groq como principal nela.
  - *Thinking* no nível mínimo nos papéis de produção, pela latência. Os tokens de raciocínio são cobrados como
    saída e somem no orçamento sem que se perceba
    ([CloudZero](https://www.cloudzero.com/blog/gemini-pricing/)). Na chamada real, o nível mínimo não gerou
    nenhum token de raciocínio.
  - Amostragem fixa: a biblioteca retira a temperatura da requisição desse modelo, e a de `roles.py` só vale nos
    outros provedores.
  - O `content` da resposta é uma lista de blocos, com a assinatura do raciocínio no bloco de texto, e não uma
    string. Quem lê o texto usa `message.text`, e o histórico volta ao modelo com a mensagem inteira, para a
    assinatura acompanhar as chamadas de ferramenta.
- **Compatível com a OpenAI** (`ChatOpenAI` com `base_url`): **uma** integração para três destinos:
  - **Groq**: cota gratuita por modelo, rápido. É a **reserva** da produção, com `openai/gpt-oss-120b` e
    `openai/gpt-oss-20b` (em produção no Groq, com ferramentas e sem chamadas paralelas). A `base_url` é uma
    constante no código ([comparação de APIs gratuitas](https://openrouter.ai/blog/tutorials/free-llm-apis-compared/)).
    O raciocínio fica em `reasoning_effort="low"`, pelo mesmo motivo do nível mínimo no Gemini, medido em chamada
    real (`decisoes-arquitetura.md`);
  - **Ollama** local: comparação nos evals (num subconjunto, porque CPU é lenta) e desenvolvimento sem internet;
  - OpenRouter e OpenAI: possíveis, sem uso previsto.
- **Anthropic** (`langchain-anthropic`): configurado e coberto por teste da fábrica, sem chamada real, porque não há
  crédito de API.

**Mudança em relação ao plano:** a reserva deixa de ser o Ollama e passa a ser o Groq. Um modelo local em CPU gera
cerca de 5 a 10 tokens por segundo, lento demais para socorrer uma conversa ao vivo, e não roda no Hugging Face
Spaces gratuito. O Ollama continua no projeto, para comparar modelos abertos nos evals e para provar que a mesma
integração atende qualquer servidor compatível com a OpenAI.

### Resiliência

Os middlewares do LangChain 1.x já existem ([docs](https://docs.langchain.com/oss/python/langchain/middleware/built-in));
não vamos reescrevê-los.

| Situação | O que acontece |
| --- | --- |
| Erro passageiro (5xx, timeout, conexão, 429) | `with_retry` em `RoleModels.runnable()`: 2 novas tentativas, espera exponencial com jitter, só nas classes repetíveis do `langchain_core` (e nos erros de rede do `httpx`, que o Gemini deixa passar sem tradução) |
| Provedor fora ou cota diária esgotada | `ModelFallbackMiddleware` no agente (com os dois modelos de `RoleModels`), `with_fallbacks` nos outros papéis: Gemini → Groq |
| `429` com tempo pedido | `ProviderGate`: o provedor fica em pausa pelo `Retry-After` (ou o `retryDelay` do Gemini), 5 s se ilegível, no máximo 1 h. Até 2 s restantes a chamada espera; acima disso é recusada na hora e vai para a reserva |
| Todos falharam | Modo degradado: `call_or_degrade` devolve `DegradedMode(motivo)`, e o chamador mostra o texto fixo honesto ("estou com instabilidade") e transfere para humano. Nunca uma ação |
| Muitas requisições por minuto | Um `InMemoryRateLimiter` por provedor, compartilhado entre os papéis, abaixo do limite gratuito. Metade do RPM é rajada, disponível desde a subida, e a outra metade é reposição, então nenhum minuto passa do RPM (`per_minute_limiter`) |
| Chamadas demais num turno | `ModelCallLimitMiddleware` e `ToolCallLimitMiddleware` (limites do tema 2) |
| Chamada lenta | Timeout por tentativa e **prazo total** por papel (`agent`: 20 s e 45 s; `writer`: 10 s e 25 s; os demais: 30 s e 90 s) |

**Uma camada só de novas tentativas.** As novas tentativas internas dos SDKs ficam desligadas (`max_retries=0` na
OpenAI e na Anthropic, `1` no Gemini, onde `0` significa "padrão do SDK"). Com as duas camadas seriam até 9 pedidos
por chamada, e o orçamento contaria 3. Cada tentativa do `with_retry` passa pelo callback do governor. O
`ModelRetryMiddleware` do agente (change 12) também chama o modelo de novo e por isso também passa pelo callback.

**O motivo do modo degradado reflete o principal.** Quando o principal e a reserva falham, o `with_fallbacks` relança
o erro do principal, e o `motivo` do `DegradedMode` sai dele, mesmo que a reserva tenha falhado por outra causa.

**Nenhuma chamada síncrona.** O `invoke` síncrono engole a recusa do governor (o `_run_coros` do `langchain_core`
registra o erro do handler assíncrono e segue), então a chamada sairia sem reserva e sem linha de uso. Por isso nada
em `src/` chama `invoke`, `stream`, `batch`, `batch_as_completed`, `generate` nem `predict` num modelo; só as
variantes `a…`. Um teste de arquitetura confere (`decisoes-arquitetura.md`, tema 3).

**Quem chama não pode ter transação aberta:** a reserva e o registro são transações curtas, antes e depois da
chamada, e a chamada leva segundos.

### Saída estruturada

- **A `ActionProposal` é um modelo Pydantic** passado como `response_format` do `create_agent`.
  - O LangChain usa o JSON Schema nativo do provedor quando existe e cai para tool calling quando não existe.
  - A resposta é **sempre validada** pelo Pydantic.
- **Se a validação falhar:** uma nova tentativa com o erro no contexto. Se falhar de novo, transferência para
  humano. Proposta inválida nunca vira ação.

### Custo e orçamento

- **Cada chamada registra:**
  - papel, provedor e modelo;
  - versão do prompt;
  - tokens de entrada, de saída e de raciocínio, lidos do `usage_metadata` da mensagem;
  - latência e custo.

  O registro vai para a tabela `llm_calls` e para o span do OpenTelemetry (tema 9).
- **Dois custos:**
  - o **real** (zero na cota gratuita);
  - o **equivalente em preço de lista**, pela tabela em `pricing.py`.

  O segundo é o número que interessa ao negócio: "isto custaria R$ X por mil conversas".
  - Os valores ficam em **micros de dólar** (inteiro), porque uma chamada custa frações de centavo e o `Money` é em
    centavos de real; a conversão é do relatório. Token ausente ou modelo sem preço deixa o custo **vazio, nunca zero**.
  - Os tokens de raciocínio já estão em `output_tokens`: o custo não os soma de novo. O desconto de token em cache do
    provedor não entra, e o custo equivalente fica um pouco acima do real (erra para cima).
- **Orçamento diário por provedor** no Postgres (requisições e tokens), conferido antes de cada chamada. Estourou →
  modo degradado.
  - A conferência é uma **reserva atômica** de uma instrução só (`INSERT … ON CONFLICT … DO UPDATE … WHERE …
    RETURNING`): sem linha de volta, a chamada é recusada. Não há leitura seguida de escrita.
  - Uma reserva nunca é devolvida, e os tokens somam depois da resposta, então chamadas em andamento podem passar um
    pouco do limite de tokens. O teto rígido é o de requisições.
  - O dia é **UTC**; o orçamento é o nosso teto, abaixo da cota do provedor, e não uma cópia dela.
  - Ao cruzar 80% do limite, o sistema registra `llm.budget_warning` uma vez por tipo de limite.
  - Banco fora do ar: a chamada é recusada.
  - **Como o registro é feito:** um `AsyncCallbackHandler` (o *governor*), um por modelo, passado em `callbacks=` do
    construtor do modelo real. Ele reserva em `on_chat_model_start`, grava a linha em `on_llm_end` e em
    `on_llm_error` e avisa a pausa do provedor. Como o objeto continua sendo a classe real do provedor, `bind_tools`
    e `with_structured_output` seguem funcionando. A `latency_ms` é o tempo visto pelo chamador, **incluindo a fila
    local** do limitador de requisições.

### Prompts versionados

- **Os prompts ficam em arquivos no repositório:** `src/deliverius/agent/prompts/<nome>/v<N>.md`, e os dos evals em
  `evals/prompts/`.
  - Cada arquivo tem um cabeçalho entre linhas `---` com `version`, `role` e `changed`, lido à mão (sem PyYAML) e
    nunca enviado ao modelo. Cabeçalho inválido, nome com `..` ou versão inexistente são erros na carga.
  - Prompt novo é **arquivo novo**, não edição do antigo.
- **Toda chamada registra versão e hash.** Assim, um resultado de eval sempre aponta para o prompt exato que o gerou.
  O hash é o SHA-256 do arquivo inteiro, e o nome, a versão e o hash chegam ao governor pelo `metadata` do `config`
  (`call_metadata`), junto de `conversation_id` e `sandbox_id`. Chamada sem prompt fica com as colunas vazias.
- **Prompts em português.** São conteúdo de produto, como texto de interface, e o cliente fala português. Os
  identificadores continuam em inglês.
- **Mudar um prompt exige rodar os evals de novo.** O relatório compara com a versão anterior; é o teste de
  regressão do prompt.

### Cache (só evals e geração de dados)

- O cache é o `SqliteResponseCache` do projeto (um `BaseCache` com o `sqlite3` da biblioteca padrão), com chave por
  prompt, modelo, parâmetros, ferramentas **e número da tentativa**. Só o hash do pedido e a resposta ficam no arquivo.
  - Não usamos o `SQLiteCache` do LangChain porque ele vive no `langchain-community`, que traz o `langchain-classic`,
    por umas 50 linhas.
- **Repetir um eval já rodado não gasta cota.** Com a cota gratuita, é o que torna os evals viáveis.
- **O número da tentativa fica na chave** porque o pass^k exige k tentativas independentes. Sem ele, o cache
  devolveria a mesma resposta k vezes e o número seria falso.
- **Desligado em produção:** cada cliente recebe uma resposta nova.
  - Todo modelo sem cache nasce com `cache=False` explícito, para nenhum cache global ter efeito, e nada em `src/`
    chama `set_llm_cache` (um teste confere). Quem pede o cache é o código de `evals` e `datagen`, passando o objeto.
  - **Em modelo com cache, o orçamento conta e não barra:** o callback roda antes da busca no cache, então não sabe
    se será um acerto. Um acerto (`response_metadata["llm_cache_hit"]`) não conta nem gera linha; uma chamada real
    soma a requisição e os tokens depois. O `429` do provedor continua sendo a barreira.

### Privacidade nos provedores

- **Só texto mascarado chega a qualquer provedor** (tema 2, `mask_input`).
- **A cota gratuita do Gemini pode usar o conteúdo enviado para melhorar os produtos do Google.** Como todo dado do
  projeto é sintético, isso é aceitável, mas fica registrado em `seguranca.md`. A política do Groq também será
  conferida (tema 8).

### Alternativas consideradas

| Opção | Por que não |
| --- | --- |
| **Contrato próprio com adaptadores** (decisão anterior) | Repete o `BaseChatModel` e não conversa com o `create_agent`. O que tinha valor, a comparação entre provedores, continua: cada papel aponta para qualquer provedor |
| **LiteLLM** como camada única | Mais uma dependência entre o LangChain e o provedor, com seu próprio formato de erro e de uso. Não acrescenta nada que as integrações do LangChain não tenham |
| **Ollama como reserva de produção** | Lento em CPU e indisponível no Spaces gratuito |
| **Prompts no Langfuse** (gestão de prompts) | Põe um serviço externo no caminho de cada conversa. Arquivos versionados no git são revisáveis em PR e funcionam sem rede |

## Busca nas políticas (RAG)

Tema 4, decidido em 2026-10-31. O domínio e os documentos estão em `modelo-dominio.md`.

### Indexação

- **Trechos por seção do markdown** (títulos de nível 2 e 3), cada um com algumas centenas de tokens. Cada trecho
  recebe:
  - o título do documento e da seção **no próprio texto**, para que um trecho solto ainda diga de onde veio;
  - um ID citável (`POL-03#prazo`).
- **Os marcadores de números são preenchidos na indexação**, a partir de `policy/limits.py`.
- **Um comando da CLI reindexa,** e o hash de cada documento evita reprocessar o que não mudou.

### Busca híbrida

| Parte | Como |
| --- | --- |
| Lexical | Full-text do Postgres com uma configuração `pt_unaccent` (o dicionário `portuguese` mais o `unaccent`, para "cancelamento" achar "cancelar" e "não" achar "nao"); coluna `tsvector` gerada e índice GIN. Os termos da consulta são unidos por **OU** (`plainto_tsquery` com `&` trocado por `|`), porque com E nenhuma seção contém todas as palavras de uma pergunta em linguagem natural (medido: recall@3 0) |
| Semântica | pgvector com `multilingual-e5-small` (384 dimensões, prefixos `query:` e `passage:` exigidos pelo E5) |
| Fusão | **RRF** (Reciprocal Rank Fusion, k = 60) dos 5 primeiros de cada lado, em SQL puro; vão os 3 melhores para o LLM |
| Índice vetorial | **Nenhum, de propósito.** São cerca de 60 trechos, e a busca exata é instantânea e não perde resultado. Um HNSW só se paga com milhares de linhas; esse limite fica anotado |

**Por que híbrida:**

- A lexical acerta termos exatos e números ("estorno", "R$ 50", "POL-07").
- A semântica acerta paráfrases ("veio gelado" → qualidade).
- O RRF combina posições sem precisar calibrar as notas dos dois lados, que estão em escalas diferentes
  ([ParadeDB](https://paradedb.com/blog/hybrid-search-in-postgresql-the-missing-manual)).

### Embeddings sem PyTorch

- **O `multilingual-e5-small` roda em ONNX** com `onnxruntime` e `tokenizers`, e a aplicação continua sem torch
  (tema 1). Usamos o `onnx/model.onnx` que o próprio autor publica, numa revisão fixa
  (`614241f622f53c4eeff9890bdc4f31cfecc418b3`), com o SHA-256 de cada arquivo conferido antes de carregar; exportar por
  nós exigiria PyTorch e uma conta para publicar o mesmo arquivo. `python -m deliverius.cli retrieval fetch-model`
  baixa e confere; a aplicação nunca baixa em execução.
- **Pacotes prontos ficaram de fora:**
  - o `sentence-transformers` traz o torch;
  - o `fastembed` teve problemas nos arquivos ONNX dos modelos E5
    ([issue](https://github.com/sourcery-ai-experiments/fastembed-rs/pull/6/files)) e não confirmei se dispensa o
    torch.

### Respostas com fundamento

- **O `answer_faq` só pode citar IDs que vieram na busca,** e o `validate_reply` confere isso.
- **Se a busca não for confiável,** a resposta é "não encontrei essa regra" e a conversa vai para um humano, em vez
  de inventar. "Não confiável" quer dizer: a maior similaridade semântica ficou abaixo de **0,85**, o limiar medido
  (98% dos pares confiáveis, 63% das perguntas fora do escopo recusadas). A regra "nenhum trecho dos dois lados" foi
  medida e descartada: só 82% dos pares têm trecho nas duas listas. Números em `busca-politicas.md`.

### Avaliação (mini-pesquisa)

Com os cerca de 60 pares pergunta → trecho, comparar quatro variantes em **recall@3** e **MRR**:
1. só lexical;
2. só semântica;
3. híbrida;
4. híbrida com um modelo de embedding maior (`bge-m3` ou `Qwen3-Embedding-0.6B`), se o tempo permitir.

A tabela vai para o README e responde, com números, à pergunta "por que busca híbrida?".
