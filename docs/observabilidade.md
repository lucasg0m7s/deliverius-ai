# Observabilidade e custo — Deliverius AI

Tema 9 da etapa de arquitetura, decidido em 2026-10-31. Define:
- o que vira **trace**, **log** e **métrica**;
- como uma conversa é seguida de ponta a ponta;
- de onde saem os números do painel de operação;
- os alertas de orçamento.

## 1. Três sinais, três destinos

| Sinal | Para quê | Onde fica | Retenção |
| --- | --- | --- | --- |
| **Traces** | Entender **uma** conversa: que nós rodaram, que ferramentas, que prompts, quanto custou cada passo | **Langfuse Cloud** (plano gratuito: cerca de 50 mil unidades por mês e 30 dias de dados) | 30 dias |
| **Logs** | Eventos e erros da aplicação | Saída padrão em JSON (structlog), coletada pela plataforma de hospedagem | Da plataforma |
| **Métricas de negócio e operação** | O painel: automação, custo, latência, fraude | **O nosso Postgres** (tabelas do domínio, `llm_calls` e visões SQL) | 90 dias (tema 8) |

**Por que as métricas não ficam no Langfuse nem no Prometheus:**
- **O Langfuse guarda só 30 dias** e é uma ferramenta de depuração, não a fonte da verdade do negócio.
- **O Prometheus exigiria mais um serviço** para hospedar.
- **Os dados já estão no Postgres:** conversas, ações, aprovações, transferências, chamadas de LLM. Visões SQL respondem tudo com um processo só e de graça. Se um dia houver vários processos, as mesmas medidas viram métricas OpenTelemetry sem mudar o painel.

## 2. Traces

### Configuração

- **Um único `TracerProvider` do OpenTelemetry,** montado em `observability/` pelo `bootstrap`.
- **O SDK do Langfuse na versão 4** recebe esse provider (`tracer_provider=...`). Uma fonte informa que o Langfuse Cloud passa a aceitar só a v4 a partir de 16/11/2026 ([upgrade path](https://langfuse.com/docs/observability/sdk/upgrade-path)), então **não usamos a v3**.
- **Instrumentações automáticas:** FastAPI, httpx e o driver do banco.
- **LangGraph e LangChain:** o `CallbackHandler` do Langfuse, passado no `config` de cada execução. Ele registra cada chamada de modelo com tokens, cada ferramenta e cada nó.
- **Desenvolvimento e testes:** o tracing fica desligado nos testes. No desenvolvimento, ele vai para um projeto separado do Langfuse (`environment="development"`).

### Granularidade e identificadores

| Conceito | Mapeamento |
| --- | --- |
| **Trace** | **Um turno**: uma execução do grafo para uma mensagem do cliente |
| **Sessão** (`session_id`) | A **conversa** (= `thread_id` do LangGraph). O Langfuse agrupa todos os turnos |
| **Usuário** (`user_id`) | Um **pseudônimo** do cliente (hash com sal), nunca o ID real nem dado pessoal |
| **Retomada após aprovação** | Um trace novo na mesma sessão, com *link* de span para o turno que pausou |
| **Eval** | Os traces dos evals ficam com a *tag* da execução e recebem *scores* (sucesso da tarefa, notas do juiz). Do relatório dá para abrir o diálogo exato |

### O que cada span carrega

- **Nos nossos nós, os nomes das convenções de GenAI do OpenTelemetry** (`gen_ai.operation.name` como `invoke_agent` ou `execute_tool`; modelo; tokens). Elas ainda estão em desenvolvimento ([Greptime](https://greptime.com/blogs/2026-05-09-opentelemetry-genai-semantic-conventions)), então os nomes ficam num só arquivo, fáceis de atualizar.
- **Atributos próprios em `deliverius.*`:**
  - intenção e confiança;
  - caminho do roteamento;
  - veredito da política;
  - nota de fraude;
  - tipo e valor da ação;
  - `sandbox_id`;
  - versão e hash do prompt;
  - provedor efetivo (se houve *fallback*);
  - custo real e equivalente.

### Privacidade nos traces

1. **O conteúdo já chega mascarado,** porque o `mask_input` é o primeiro nó (tema 2).
2. **Segunda barreira:** o `mask` e o `mask_otel_spans` do Langfuse aplicam de novo o `privacy.mask` antes de exportar.
3. **Turnos do caminho sensível** (saúde) são marcados, e o exportador **troca o conteúdo** por "[conteúdo sensível omitido]". O relato de saúde não sai do sistema nem para o trace (`seguranca.md` 3.2).
4. **Os prompts de sistema não são enviados inteiros a cada trace.** Vão a versão e o hash; o texto está no repositório.

## 3. Logs

- **Formato:** structlog, em JSON em produção e legível no desenvolvimento.
- **Todo log tem `trace_id` e `span_id`.** O mesmo `trace_id` volta ao cliente no cabeçalho `X-Trace-Id` e aparece nos *problem details* de erro. Um usuário que relata "deu erro" entrega o ID, e dele se chega ao trace e aos logs.
- **O `trace_id` também é gravado** em `llm_calls`, `actions`, `approval_requests` e `audit_log`. Da linha de auditoria de um reembolso chega-se ao trace que o gerou.
- **Níveis:**
  - `INFO` para eventos de negócio (ação executada, transferência, *fallback*, modo degradado);
  - `WARNING` para orçamento acima de 80% e falha do `validate_reply`;
  - `ERROR` só para o que exige olhar.
- **O filtro de mascaramento no logger** é a segunda barreira (tema 6).
- **Eventos da camada de LLM** (change `camada-llm`), todos sem texto de mensagem nem de erro do provedor, só o tipo
  da exceção e campos de identificação:

| Evento | Nível | Quando | Campos |
| --- | --- | --- | --- |
| `llm.budget_exhausted` | `WARNING` (`ERROR` com o banco fora) | A chamada foi recusada por falta de orçamento, ou porque o banco do orçamento não responde | `provider`, `reason` |
| `llm.budget_warning` | `WARNING` | Uma vez por tipo de limite, ao cruzar 80% | `provider`, `kind` (`requests` ou `tokens`), `used`, `limit` |
| `llm.provider_paused` | `WARNING` | Um `429` abriu a pausa (`state=opened`) ou uma chamada foi recusada por ela (`state=refused`) | `provider`, `seconds` ou `seconds_left` |
| `llm.call_failed` | `WARNING` | Uma tentativa falhou | `provider`, `role`, `error_type` |
| `llm.fallback_used` | `WARNING` | O principal falhou e a reserva assumiu | `role`, `from_provider`, `to_provider` |
| `llm.degraded` | `WARNING` | Todos falharam, o orçamento acabou ou o prazo estourou: modo degradado | `role`, `reason`, `error_type` |
| `llm.usage_record_failed` | `ERROR` | A gravação do uso falhou; a resposta já obtida foi devolvida | `provider`, `step` (`request`, `tokens` ou `call`), `error_type` |
| `llm.price_missing` | `WARNING` | O modelo não tem preço na tabela: o custo equivalente fica vazio | `provider`, `model` |

  Viram as métricas do painel: modo degradado, uso do orçamento e *fallback*. A `latency_ms` de `llm_calls` é o
  tempo visto pelo chamador, **incluindo a fila local** do limitador de requisições.

## 4. Painel de operação: as métricas e de onde saem

Todas vêm de **visões SQL** no Postgres, expostas pela API (`/metrics/...`, papel `reviewer`) e mostradas na demo
(tema 10). Cada número traz o período e o filtro de provedor.

| Grupo | Métrica | Definição |
| --- | --- | --- |
| **Resultado** | Taxa de automação | Conversas resolvidas sem transferência nem aprovação humana ÷ conversas encerradas |
| | Taxa de transferência, por motivo | Sensível, limite estourado, política negou duas vezes, pedido do cliente, modo degradado |
| | Taxa de "não sei" | Turnos que caíram em `ask_clarification` |
| **Custo** | Custo por conversa (real e equivalente) | Soma de `llm_calls` por conversa, mais a média e o p95 |
| | Custo por mil conversas, por provedor e por papel | O número de negócio |
| | Orçamento usado hoje | Requisições e tokens ÷ orçamento, por provedor |
| **Latência** | p50 e p95 por turno e por nó | Dos spans dos nós, gravados por turno em `llm_calls` e na tabela de turnos |
| **Qualidade** | Falhas do `validate_reply`; taxa de *fallback*; vezes em modo degradado | — |
| | Distribuição de intenções por dia | Para ver mudança de padrão |
| **Aprovação e fraude** | Aprovações: abertas, tempo até decidir (p50/p95), taxa de aprovação | — |
| | **Valor retido**: reembolsos que foram para análise e acabaram recusados | A "fraude barrada" em reais |
| | **Falsos alarmes**: foram para análise e acabaram aprovados | O custo do antifraude para o cliente honesto |
| | Distribuição da nota de fraude por semana | Extra: alerta de deriva por PSI |

**O painel mede o produto; os evals medem a qualidade.** As métricas de qualidade com gabarito (pass^k, violações
de política) vêm dos relatórios de eval (tema 7). O painel mostra o que se mede **sem** gabarito, em operação.

## 5. Alertas e saúde

Não há plantão nem ferramenta de *paging*: é um projeto de portfólio. Os "alertas" são visíveis e automáticos.

| Condição | Reação |
| --- | --- |
| Orçamento de um provedor ≥ 80% | Log `WARNING` e aviso no painel |
| Orçamento ≥ 100% | O provedor sai do roteamento: reserva ou modo degradado (tema 3), registrado |
| *Fallback* ou modo degradado | Contados no painel; cada ocorrência é um evento de log |
| Aprovação pendente acima do prazo prometido ao cliente | Destaque na fila do console |

**Saúde do serviço:**
- `/health` (*liveness*): o processo responde.
- `/ready` (*readiness*): banco acessível, modelos carregados e esquema de features conferido (tema 5).

O deploy só considera a nova versão pronta quando o `/ready` passa.

## 6. Alternativas consideradas

| Opção | Por que não |
| --- | --- |
| **LangSmith** | É a ferramenta da própria LangChain e se integra sem esforço, mas o plano gratuito é limitado e ela não aceita OpenTelemetry tão abertamente. O Langfuse é código aberto, recebe OTLP e pode ser hospedado por nós se preciso |
| **Só logs** | Não mostram a árvore de uma conversa: qual nó, qual ferramenta, quanto cada passo custou |
| **Prometheus e Grafana** para as métricas | Mais dois serviços para hospedar de graça, sem ganho com um processo só |
| **Métricas no Langfuse** | Retenção de 30 dias, e o painel do produto não deve depender da ferramenta de depuração |
| **SDK v3 do Langfuse** | O Cloud passa a exigir a v4 (segundo fonte secundária) |
