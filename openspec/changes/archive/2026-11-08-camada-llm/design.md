## Context

Motivação em `proposal.md`, requisitos em `specs/llm/spec.md`. O contrato (`BaseChatModel`, sem camada própria por cima),
os papéis e a tabela de resiliência já estão em `docs/arquitetura.md` e não são rediscutidos aqui.

O estado atual que molda o desenho:

- Não existe `llm/`, `bootstrap.py` nem tabela de conversas. A `llm_calls` nasce sem chave estrangeira para ela.
- O `.env` não tem chaves de provedor. A cota gratuita do Gemini não é publicada em números: a página de limites manda
  para o AI Studio.

**Protótipo descartável**, fora do repositório, com `langchain-core` 1.7.3, `langchain-openai` 1.7.3,
`langchain-google-genai` 4.4.2 e `langchain-anthropic` 1.7.7, usando modelos falsos e um servidor simulado por
`httpx.MockTransport`. Nenhuma chamada real foi feita.

| Pergunta | Resultado | Como se sabe |
| --- | --- | --- |
| Exceção num callback com `raise_error=True` em `on_chat_model_start` impede a chamada? | Sim: zero chamadas ao modelo | Medido |
| `with_fallbacks` pega essa exceção? | Sim: a reserva responde | Medido |
| `metadata` do `config` chega ao callback? | Sim | Medido |
| O callback dispara num acerto de cache? | Sim, `on_chat_model_start` e `on_llm_end`. Por isso o marcador da seção "Cache" | Medido |
| Novas tentativas do SDK passam pelo callback? | Não: 3 pedidos HTTP, um só `start`. Por isso ficam desligadas | Medido |
| `with_retry` passa pelo callback? | Sim: 3 chamadas, 3 pares de eventos | Medido |
| Cancelamento dispara `on_llm_error`? | Não: só `start`. Por isso o registro de execuções tem limite | Medido |
| Ferramentas mudam a chave do cache? | Sim | Medido |
| A chave de API entra no `llm_string`? | Não, medido com um valor único no `ChatOpenAI`. As outras integrações são conferidas nas tarefas | Medido |
| O `429` expõe o cabeçalho? | Sim, em `error.response.headers`, e o erro é um `ModelRateLimitError` (compatível com a OpenAI) | Medido |
| Tokens de raciocínio | Já somados em `output_tokens`, com o detalhe em `output_token_details.reasoning` | Medido na compatível com a OpenAI, lido no código do Gemini |
| `max_retries` no Gemini | O padrão é 6, e `1` desliga: `0` significa "padrão do SDK" | Documentação da biblioteca |
| O SDK do Gemini lê `Retry-After`? | Não há referência a ele nos módulos de cliente e de erros. Supõe-se o tempo em `retryDelay`, nos detalhes do erro encadeado | **A conferir na implementação** |
| `stream_usage` com `base_url` própria | Fica `None`, e por isso é ligado explicitamente | Medido |

**Provedores conferidos em 2026-11-02:**

| Modelo | Situação | Preço de lista (US$ por 1M, entrada/saída) | Limites publicados |
| --- | --- | --- | --- |
| `gemini-3.5-flash-lite` | Estável. Ferramentas, saída estruturada e raciocínio (`minimal`, o padrão, até `high`). Gratuito na cota | 0,30 / 2,50, com raciocínio no preço de saída | Só no AI Studio. O RPD volta à meia-noite do Pacífico |
| `openai/gpt-oss-120b` (Groq) | Produção, com ferramentas, sem chamadas paralelas | 0,15 / 0,60 | 30 RPM, 1.000 RPD, 8.000 TPM e 200.000 TPD na página de limites, sem dizer o plano. A página de modelos diverge. Vale `console.groq.com/settings/limits` |
| `openai/gpt-oss-20b` (Groq) | Igual | 0,075 / 0,30 | Igual |

Fontes: [modelo](https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite),
[preços](https://ai.google.dev/gemini-api/docs/pricing), [limites](https://ai.google.dev/gemini-api/docs/rate-limits),
[raciocínio](https://ai.google.dev/gemini-api/docs/thinking), [modelos do Groq](https://console.groq.com/docs/models),
[limites do Groq](https://console.groq.com/docs/rate-limits).

## Goals / Non-Goals

**Goals:**

- API pequena, que `datagen` (6), `agent` (12) e `evals` (15) usam sem mudança, inclusive com o `create_agent`.
- Falhar para o lado seguro: sem certeza sobre o orçamento, não chama; sem LLM, degrada e não age.
- Nenhum conteúdo de mensagem em registro, log ou mensagem de erro.

**Non-Goals:**

- Alinhar o dia do orçamento ao dia de cada provedor (ver "Dia em UTC").
- Medir a qualidade de um modelo: isso é da change 15.
- Chamada síncrona: toda a aplicação é `async`.

## Decisions

### API pública

```python
LlmRuntime.build(settings, session_factory, clock)      # falha se faltar configuração
runtime.models_for(role, *, cache=None) -> RoleModels   # primary, fallback | None
RoleModels.runnable() -> Runnable                       # tentativas e reserva, para quem não usa middleware
await call_or_degrade(runnable, messages, role, config) -> LlmReply | DegradedMode
load_prompt(directory, name, version) -> Prompt         # text, version, content_hash
call_metadata(prompt, conversation_id, sandbox_id) -> dict  # entra no `config`
```

`RoleModels` entrega os dois modelos porque o `with_fallbacks` devolve um `Runnable`, e o `create_agent` precisa de um
`BaseChatModel`: na change 12, o agente passa os dois ao `ModelFallbackMiddleware`, e os outros papéis usam `runnable()`.

Arquivos como em `arquitetura.md`, seção "Camada de LLM", mais `ports.py`, `repository.py`, `in_memory.py`, `governor.py`
(o callback) e `errors.py`.

### Orçamento e registro num callback, não num invólucro do modelo

Um `AsyncCallbackHandler` com `raise_error=True`, um por modelo, passado no `callbacks=` do construtor:

- `on_chat_model_start`: confere a pausa do provedor e faz a reserva. Uma recusa lança `LlmBudgetExceededError` ou
  `ProviderPausedError`.
- `on_llm_end`: grava `llm_calls` e soma os tokens, numa transação curta. Uma falha aqui é registrada e não sobe.
- `on_llm_error`: grava a linha de erro, só com o tipo da exceção, e abre a pausa num `429`.

O callback continua ativo em `bind_tools` (medido), e o objeto continua sendo a classe real do provedor, de onde o LangChain
lê as capacidades para a saída estruturada nativa. `with_structured_output` e os subgrafos do `create_agent` entram nos
testes desta change e da change 12.

**Alternativas descartadas:**

- *Subclasse de `BaseChatModel` que embrulha o provedor:* teria de repassar `bind_tools` e `with_structured_output`, e a
  saída estruturada nativa se perderia. É o contrato próprio que o tema 3 retirou.
- *Middleware do `create_agent`:* só cobre o agente, e não `writer`, `datagen` nem os evals.
- *Mixin sobre `_agenerate`:* acertaria o momento exato da chamada real, mas sobrescreve método privado de três integrações
  e deixa de fora o `_astream`.

**Limites conhecidos:**

- Cancelamento não dispara `on_llm_error`, então o registro de início por `run_id` é limitado a 1.024 execuções, com a mais
  antiga descartada. Uma chamada cancelada não gera linha, e a reserva fica contada, o que erra para o lado seguro.
- **A latência registrada inclui a fila local.** O limitador de requisições espera dentro da chamada, depois do
  `on_chat_model_start`, então `latency_ms` é o tempo visto pelo chamador, não só o da rede.
- **O motivo do modo degradado reflete o principal.** Com tudo falhando, o `with_fallbacks` relança o primeiro erro, o do
  principal, e dele sai o motivo do `DegradedMode`.
- **Chamada síncrona não é governada (medido na tarefa 5.8, contrariando o que se esperava).** No `invoke` síncrono, o
  `_run_coros` do `langchain_core` registra "Error in callback coroutine" e engole a exceção do handler assíncrono, qualquer
  que seja o `raise_error`; o handler ainda roda num event loop novo, de modo que um recurso preso ao loop principal (a
  conexão do Postgres) também falha. O resultado é fail-open: a recusa não impede a chamada, que sai sem reserva e sem
  linha em `llm_calls` (o `on_llm_end` não acha a execução). No `ainvoke`, a recusa lança, como projetado. **Regra
  adotada:** nenhum código de `src/` chama `invoke`, `stream`, `batch`, `batch_as_completed`, `generate` nem `predict`
  num modelo ou `Runnable`; só as variantes `a…`. Um teste de arquitetura percorre o AST de `src/deliverius/` e falha
  com qualquer uma. O teste `test_a_synchronous_invoke_bypasses_the_async_governor_…` fixa o comportamento medido, para
  uma mudança de versão do LangChain aparecer.

### Reserva atômica

```sql
INSERT INTO llm_budget_usage (provider, day, requests_used, tokens_used)
SELECT :provider, :day, 1, 0 WHERE :max_requests > 0
ON CONFLICT (provider, day) DO UPDATE SET requests_used = llm_budget_usage.requests_used + 1
  WHERE llm_budget_usage.requests_used < :max_requests AND llm_budget_usage.tokens_used < :max_tokens
RETURNING requests_used
```

Sem linha de volta, a chamada é recusada. É uma instrução só, então não há leitura seguida de escrita, e a linha fica travada
só pelo tempo do comando.

- **Sem devolução em erro.** O provedor pode já ter contado o pedido, e devolver exigiria um caminho a mais que pode falhar.
- **Tokens depois da resposta.** Chamadas em andamento podem passar um pouco do limite de tokens. O teto rígido é o de
  requisições, e os valores padrão têm folga.
- **Aviso de 80%:** uma vez, quando a requisição reservada é exatamente a que cruza; nos tokens, quando a soma cruza. Sem
  estado em memória.
- **Banco fora do ar:** recusa, e a reserva também recusa. O resultado é o modo degradado.

**Dia em UTC.** O Gemini volta à meia-noite do Pacífico. Alinhar exigiria um fuso por provedor e o pacote `tzdata` no Windows, e
o Groq nem publica o momento. O orçamento é o **nosso** teto, abaixo da cota, e não uma cópia dela: se as janelas não
coincidirem, o `429` do provedor cai na pausa e na reserva, o mesmo caminho de qualquer falha.

**Padrões:** Gemini 400 requisições e 2.000.000 de tokens por dia; Groq 800 e 150.000. São valores conservadores, a confirmar na
conta. Ollama sem limite (conta, não barra). Anthropic com zero, de modo que nenhuma chamada real acontece por engano.

### Uma camada só de novas tentativas

SDKs desligados (`max_retries=0` em OpenAI e Anthropic, `1` no Gemini), e o `with_retry` em `RoleModels.runnable()`, com 3
tentativas em `ModelRateLimitError`, `ModelAPIError`, `ModelConnectionError` e `ModelTimeoutError`, as classes que a
`langchain_core` marca como repetíveis.

- **Por quê:** com as duas camadas, seriam até 9 pedidos, e o orçamento veria 3. Cada tentativa passa pelo callback.
- **Alternativa descartada:** deixar nos SDKs. O orçamento contaria menos do que sai, e o Gemini não aparenta ler o `Retry-After`.
- `arquitetura.md` já previa o `ModelRetryMiddleware` no agente; ele chama o modelo de novo, então também deve passar pelo
  callback, o que a change 12 confere.
- **Conferido (tarefa 6.4):** a Anthropic e a integração compatível com a OpenAI traduzem 429, 5xx, timeout e conexão para
  essas classes. O Gemini traduz 429 e 5xx, mas deixa passar o timeout e o erro de conexão como `httpx.TimeoutException` e
  `httpx.NetworkError`; as duas entram na lista de repetíveis. `httpx.TransportError` ficou de fora, porque inclui
  `UnsupportedProtocol` e `ProxyError`, erros de configuração que repetir não resolve.
  `LlmBudgetExceededError` e `ProviderPausedError` não são repetíveis, então vão direto para a reserva.

### Pausa do provedor depois de um `429`

Um `ProviderGate` em memória por provedor, com `not_before` e o `Clock` injetado. O tempo vem de
`error.response.headers["retry-after"]` ou do `retryDelay` no erro do Gemini. Ilegível, negativo ou data HTTP: 5 s. Teto: 1 h.

- **Por quê:** o limitador do LangChain não aprende nada com um `429`. Sem isso, todas as conversas continuariam batendo num
  provedor que já pediu para esperar, gastando orçamento.
- **Espera de até 2 s:** é barata, e o `429` por minuto muitas vezes pede pouco. Acima disso, a reserva responde antes. O teto é
  uma constante, não uma variável.
- **Alternativa descartada:** dormir o tempo pedido. Uma cota diária pediria horas, e o turno tem 60 s.
- **Vale para uma instância,** como o limitador (`robustez.md`, seção 3).

### Limite por minuto

Um `InMemoryRateLimiter` por provedor, compartilhado entre os papéis. No código do LangChain, ele roda depois do cache, então um
acerto não espera. Padrões: Gemini 10 RPM (a confirmar no AI Studio), Groq 25, Ollama e Anthropic sem limitador.

Conferido no teste real: com os padrões da biblioteca, o balde começa vazio e guarda uma requisição, então toda chamada esperava
60/RPM segundos (6 s no Gemini, 2,4 s no Groq), e uma conversa faz várias em seguida. O `per_minute_limiter` enche o balde na
subida com metade do RPM e repõe a outra metade por minuto: a rajada de um turno sai na hora e nenhum minuto passa do RPM. A
latência medida caiu para 0,7 s no Gemini e 0,3 a 0,5 s no Groq.

### Timeouts e prazo total

Por papel em `roles.py`, em segundos por tentativa: `agent` 20, `writer` 10, `customer_sim`, `judge` e `datagen` 30. O prazo total
fica em `call_or_degrade`, com `asyncio.timeout`: 25 s no `writer`, 45 s no `agent`, 90 s nos outros, porque 3 tentativas em 2
provedores somariam 60 s só no `writer`. O prazo do turno é da change 12.

O limitador e a espera da pausa não têm timeout próprio, e por isso o prazo total existe. Ele também cobre um travamento no meio
do streaming, que o timeout do SDK pode não pegar.

### Modo degradado como resultado

`call_or_degrade` devolve `LlmReply(message) | DegradedMode(reason)`, para o chamador usar um `match` exaustivo. Captura `Exception`,
porque cada integração tem as suas, e registra `llm.degraded` com o motivo e o tipo. `CancelledError` é `BaseException` e passa. O
prazo total vira `reason="deadline"`.

**Alternativa descartada:** uma exceção `LlmUnavailableError`. Quem a esquecer leva um erro `500`, e "sem LLM" é um caso previsto: a
falha de LLM nunca vira ação (`convencoes-codigo.md`).

### Tabelas

```
llm_calls: id, created_at, conversation_id NULL, sandbox_id NULL, role, provider, model, prompt_name NULL,
  prompt_version NULL, prompt_hash NULL, status (ok, error), error_type NULL, latency_ms,
  input_tokens NULL, output_tokens NULL, reasoning_tokens NULL, real_cost_usd_micros NULL,
  equivalent_cost_usd_micros NULL, trace_id NULL
llm_budget_usage: provider, day, requests_used, tokens_used; PK (provider, day)
```

- **Sem `AccessScope`:** nenhuma consulta de dado de cliente nesta change, só `INSERT` e `upsert` de sistema. A leitura entra no E1,
  para o revisor, com escopo.
- **Dólar em micros, inteiro.** `Money` é real em centavos, e uma chamada custa frações de centavo. A conversão fica no relatório,
  com a cotação da data.
- **`NULL` não é zero:** token ausente e preço desconhecido ficam vazios, para a soma do painel não mentir.
- **Tokens de raciocínio são um detalhe,** já contidos em `output_tokens`: o custo não os soma de novo.
- **Tokens em cache do provedor** não têm desconto no custo equivalente, que fica um pouco acima do real. Aceito: mais simples, e erra
  para cima.
- **Índices:** `ix_llm_calls_created_at` (retenção da change 13 e painel) e `ix_llm_calls_conversation_id_created_at`, parcial onde
  `conversation_id IS NOT NULL` (custo por conversa). O `upsert` usa a chave primária.
- **`CHECK`:** `status` conhecido, tokens e custos não negativos, contadores não negativos.

`real_cost` é zero, salvo se o provedor estiver em `LLM_PAID_PROVIDERS`. `pricing.py` tem o preço dos três modelos da tabela acima,
com a data da consulta, em `Decimal`. Sem entrada, o custo equivalente fica vazio e `llm.price_missing` é registrado.

### Configuração

Referência compacta `provedor:modelo`, validada no `Settings`:

```
LLM_AGENT_MODEL=gemini:gemini-3.5-flash-lite        LLM_AGENT_FALLBACK=groq:openai/gpt-oss-120b
LLM_WRITER_MODEL=gemini:gemini-3.5-flash-lite       LLM_WRITER_FALLBACK=groq:openai/gpt-oss-20b
LLM_CUSTOMER_SIM_MODEL=groq:openai/gpt-oss-120b     LLM_JUDGE_MODEL=groq:openai/gpt-oss-120b
LLM_DATAGEN_MODEL=gemini:gemini-3.5-flash-lite
GEMINI_API_KEY, GROQ_API_KEY, ANTHROPIC_API_KEY     (SecretStr, opcionais)
OLLAMA_BASE_URL=http://localhost:11434/v1
LLM_<PROVIDER>_DAILY_REQUESTS, _DAILY_TOKENS, _RPM; LLM_PAID_PROVIDERS
```

- **Alternativa descartada:** quatro variáveis por papel, que seriam vinte só para dizer provedor e modelo.
- **Só as chaves dos provedores em uso são exigidas,** na montagem, e não no `Settings`: a API sobe sem LLM até a change 12, e o
  `/health` não pode depender disso. O `bootstrap` da change 12 chama a montagem na subida.
- **Temperatura, timeout e teto de saída ficam no código**, em `roles.py`: mudam junto com os prompts e os evals. Teto de saída: 1.024
  no `agent`, 512 no `writer`, 1.024 nos outros. Temperatura: 0 no `agent` e no `judge`, 0,3 no `writer`, 0,9 no `customer_sim` e no
  `datagen`. Conferido no teste real: o `gemini-3.5-flash-lite` tem amostragem fixa e a biblioteca retira a temperatura da
  requisição, então ela só vale nos outros provedores.
- **Gemini:** `thinking_level="minimal"`, confirmado na página para este modelo e na chamada real, que não gerou tokens de
  raciocínio. O teto de saída conta também os tokens de raciocínio.
- **Groq:** `reasoning_effort="low"`. O padrão do `gpt-oss` é médio e, na chamada real, gastava de 53 a 67 tokens de raciocínio
  numa saída estruturada trivial, contra 9 a 10 com `low`, sem perder acerto. A saída estruturada usa `json_schema`, o padrão
  do `langchain-openai`, e veio validada.
- **Compatível com a OpenAI:** `stream_usage=True`, `use_responses_api=False`. Groq tem `base_url` fixa no código e Ollama na
  configuração, nunca vinda de cliente ou LLM.

### Cache

Um `BaseCache` nosso, com `sqlite3` da biblioteca padrão, serializando por `langchain_core.load`, com a tentativa no construtor e na
chave (hash de prompt, `llm_string` e tentativa).

- **Por que não o `SQLiteCache`:** está no `langchain-community`, que traz o `langchain-classic`, para umas 50 linhas. `arquitetura.md`
  é atualizado.
- **Marcador de acerto:** o callback dispara também num acerto, e o `total_cost: 0` que o LangChain põe ali é detalhe interno. O cache
  marca a mensagem devolvida com `response_metadata["llm_cache_hit"]`, e o callback não registra nada (medido).
- **Em modelo com cache, o orçamento conta e não barra.** O `on_chat_model_start` roda antes da busca, então ali não se sabe se será
  acerto. Barrar quebraria "repetir um eval não gasta cota" justamente quando a cota acabou. No `on_llm_end`, uma chamada real soma a
  requisição e os tokens. O `429` do provedor continua sendo a barreira, e a pausa continua valendo.
- **Produção:** `cache=False` explícito em todo modelo sem cache, para nenhum cache global ter efeito, e nada chama `set_llm_cache`. Não
  há variável de ambiente: quem pede é o código de `evals` e `datagen`, passando o objeto. A API não tem como ligar.
- **Arquivo:** caminho dado pelo chamador, em `.cache/` no `.gitignore`. Conteúdo só sintético ou mascarado.

### Prompts

Cabeçalho `---` com `version`, `role` e `changed`, lido à mão, sem PyYAML. O hash é SHA-256 do arquivo inteiro. O nome, a versão e o
hash chegam ao callback por `metadata` do `config`, junto de `conversation_id` e `sandbox_id`. Chamada sem prompt fica com as colunas
vazias.

### Falso

`ScriptedChatModel(BaseChatModel)` próprio, porque precisa de ferramentas, erro e travamento roteirizados. Passos: texto, ferramentas,
estruturado, erro, travamento e tokens. A fábrica o aceita como provedor injetado, com o mesmo callback.

### Regras de import

- `llm` entra nas camadas, abaixo de `api` e acima de `policy | privacy`.
- Contrato novo **"provider integrations live only in llm"**: `langchain_google_genai`, `langchain_openai`, `langchain_anthropic`,
  `google.genai`, `openai` e `anthropic` proibidos fora de `llm`, com `allow_indirect_imports = true`, porque `bootstrap → llm` será
  legítimo.
- `langchain_core` proibido nos módulos atuais fora de `llm`. O contrato de utilitários passa a proibir `deliverius.llm`.

## Checklist de `docs/robustez.md`, seção 11

| Item | Resposta |
| --- | --- |
| Rota, ferramenta, `AccessScope` | Nenhuma rota nem ferramenta, então a matriz de autorização não muda. Só escritas de sistema, sem dado de cliente; a leitura fica para o E1 |
| Corrida | Orçamento em paralelo: instrução única com `WHERE`, e teste com `asyncio.gather` contra o Postgres. Soma de tokens: `SET tokens_used = tokens_used + :n`, com teste igual. Pausa do provedor: em memória, num só event loop, sem `await` entre ler e escrever |
| Idempotência | Chamada de LLM pode repetir, porque não tem efeito. Cada tentativa é uma linha nova, de propósito. Sem chave de idempotência |
| Timeout e novas tentativas | Timeout por tentativa e prazo total. Máximo de 2 novas tentativas, com espera exponencial e jitter, só em erro passageiro. `Retry-After` respeitado pela pausa |
| Recursos esgotáveis | Orçamento diário, RPM, teto de saída, 1.024 execuções abertas, pausa de no máximo 1 h. O arquivo do cache não tem limite: é local, do desenvolvedor, e apagável |
| Queda no meio | A reserva fica contada e não há linha em `llm_calls`, o que erra para o lado seguro. A chamada não tem efeito. Cancelamento se propaga, e a escrita curta é desfeita |
| Transação com rede ou LLM | Nenhuma: reserva e registro são transações próprias, antes e depois da chamada. Quem chama não pode ter transação aberta (regra escrita na docstring) |
| Eventos de log | `llm.call_failed`, `llm.fallback_used`, `llm.budget_exhausted`, `llm.budget_warning`, `llm.provider_paused`, `llm.degraded`, `llm.usage_record_failed`, `llm.price_missing`. Viram métrica no E1: modo degradado, reserva e orçamento. Sem texto do provedor |
| Texto de fora e regex | A saída do LLM volta como dado, e a validação do conteúdo é da change 12. Nenhuma regex sobre texto de cliente. O tempo pedido no `429` é validado. Texto de erro do provedor nunca é gravado, porque pode repetir o prompt |
| Grafo, banco, API | Migration aditiva, duas tabelas novas, com `downgrade` testado. Grafo e API não mudam |
| Consultas e índices | `upsert` pela chave primária; índices acima conferidos com `EXPLAIN` no teste de integração. A chave para `conversations` entra na change 13, com `NOT VALID` |
| Travas e migrations | Só uma linha do orçamento por transação, durante um comando, sem risco de ordem. Tabelas novas, sem risco de trava longa, e a migration define `lock_timeout` |
| Configuração nova | Referência inválida falha no `Settings`. Chave faltando falha em `LlmRuntime.build`, com a variável na mensagem. Tudo no `.env.example` |

## Checklist de `docs/seguranca.md`, seção 7

- Nenhum texto do cliente é gravado aqui. O que chega ao provedor é responsabilidade do `mask_input` (change 12), e a camada não lê o
  conteúdo.
- Nenhuma ferramenta nova. A saída do LLM não vira SQL, comando nem URL nesta camada.
- Chaves em `SecretStr`, nunca logadas, e um teste confere com um valor único que elas não vão para o cache.
- Destinos de rede fixos: SSRF não se aplica.

## Avaliação

Nenhum modelo é escolhido por qualidade aqui. A camada é avaliada por:

- testes com o falso e com `MockTransport`;
- integração contra o Postgres: zero estouro em 50 chamadas juntas;
- `-m llm`: uma chamada real com ferramenta e uma com saída estruturada, em Gemini e Groq, conferindo em `llm_calls` tokens maiores que
  zero e custo equivalente preenchido. Limiar: passa ou não passa.

A qualidade por provedor sai na change 15, com pass^k e intervalo de Wilson.

## Risks / Trade-offs

- [O `raise_error` faz o LangChain imprimir um aviso a cada recusa] → É ruído com o tipo e a mensagem da nossa exceção, sem conteúdo, e
  passa pelo filtro do logger.
- [Mudança de comportamento no LangChain] → Só APIs públicas: callback, `BaseCache`, `with_retry`, `with_fallbacks`. O `uv.lock` fixa a
  versão, e os testes pegam a mudança.
- [`retryDelay` do Gemini não vem onde o código espera] → Cai na pausa de 5 s e na reserva, sem quebrar.
- [Modelo do Gemini ou limites mudam] → Variável de ambiente, e tabela de preços com data.
- [Temperatura incompatível com o Gemini 3] → Conferida no `-m llm`.
- [Sem as chaves, o critério do `-m llm` não fecha] → Sem chave, o teste **falha** em vez de pular, porque só roda a pedido. A change não
  é arquivada até ele passar.

## Migration Plan

Aditiva: `alembic upgrade head` cria as duas tabelas, e a aplicação atual as ignora. Reverter é `alembic downgrade -1`, sem perda de dado
de domínio.

## Pendências humanas

Não mudam o desenho, mas bloqueiam o arquivamento:

1. Criar a chave do Gemini e conferir a cota de `gemini-3.5-flash-lite` no AI Studio (RPM, RPD), ajustando os padrões de 10 RPM e 400 por
   dia.
2. Criar a chave do Groq, ativar a retenção zero e conferir os limites em `console.groq.com/settings/limits`.
