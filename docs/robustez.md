# Robustez: o que não aparece no caminho feliz

São as preocupações que não aparecem no "caminho feliz" e que, por isso, costumam ser esquecidas: duas
requisições ao mesmo tempo, a mesma requisição duas vezes, um cliente que cai no meio, um provedor que trava,
uma cota que acaba, um texto feito para enganar, uma versão nova que retoma uma conversa antiga. Quem pode ver o
quê está em `autorizacao.md`. Cada uma tem aqui uma decisão concreta, a change que a implementa (`roadmap.md`) e um teste.

**Regra para toda change:** o `design.md` responde ao checklist do fim deste documento (seção 11) para o que a change cria.
"Não se aplica" é uma resposta válida, desde que escrita.

## 1. Condições de corrida

O sistema roda **uma instância**, mas atende várias requisições ao mesmo tempo no mesmo event loop. Toda leitura
seguida de escrita ("confere e depois faz") é suspeita até prova em contrário.

| Corrida | O que daria errado | Decisão | Change |
| --- | --- | --- | --- |
| **Dois reembolsos no mesmo pedido ao mesmo tempo** (duas conversas, ou uma aprovação retomada enquanto outro reembolso sai) | Cada um passa no teto da política sozinho; juntos, passam do teto | `execute_action` abre uma transação curta, trava o pedido (`SELECT … FOR UPDATE`), **reavalia a política com os reembolsos atuais** e só então grava. Se a reavaliação negar, não executa e a conversa recebe o motivo | 11 |
| **Aprovação decidida horas depois** | A política ou o pedido mudaram durante a pausa | A mesma reavaliação acima, na retomada. A decisão humana autoriza a proposta, não dispensa a política | 11 |
| **Duas mensagens do cliente na mesma conversa ao mesmo tempo** (clique duplo, duas abas) | Duas execuções do grafo no mesmo `thread_id`, com estado e checkpoint embaralhados | **Uma vez por conversa:** antes do turno, a API pega uma *lease* com um UPDATE condicional (`turn_lease_until < now()`). Ocupada → `409 turn_in_progress` com `Retry-After`. A lease expira sozinha (prazo do turno + folga), então uma queda não trava a conversa | 13 |
| **Mensagem nova chega enquanto a aprovação é retomada** | O grafo roda duas vezes no mesmo `thread_id` | A retomada usa a mesma lease da conversa | 13 |
| **Dois revisores decidem a mesma aprovação** | Duas decisões, talvez opostas | Trava otimista pela `version` (`UPDATE … WHERE version = ?`). A mesma decisão repetida devolve o estado atual (200); uma decisão diferente sobre uma aprovação já decidida → `409 approval_already_decided` | 11 |
| **Duas aprovações pendentes na mesma conversa** | O console mostra casos duplicados | Índice único parcial: uma `approval_request` pendente por conversa | 11 |
| **Orçamento diário de LLM consumido em paralelo** | Duas chamadas leem "sobra 1" e as duas passam | Reserva atômica antes da chamada: `INSERT … ON CONFLICT DO UPDATE SET used = used + 1 WHERE used < limite RETURNING`. Sem linha de volta → modo degradado. Os tokens são acertados depois da resposta, com `SET tokens_used = tokens_used + :n` (nenhuma soma se perde). Uma reserva nunca é devolvida: o provedor pode já ter contado o pedido. Há teste com `asyncio.gather` contra o Postgres: 50 chamadas com 10 restantes dão exatamente 10 reservas | 5 |
| **Vários `429` seguidos num provedor** | Cada conversa continua batendo num provedor que já pediu para esperar, gastando orçamento | `ProviderGate` (em memória, por provedor): depois de um `429`, o provedor fica em pausa pelo tempo pedido. Não há `await` entre ler e escrever o instante da pausa, então um event loop só não a embaralha. Uma pausa nova nunca encurta a que já corre | 5 |
| **Teto global de sandboxes da demo** | Dois visitantes passam do teto ao mesmo tempo | A mesma reserva atômica, num contador | E2 |
| **Tarefas de fundo** (retenção, limpeza das sandboxes) rodando duas vezes | Com mais de um worker ou instância, a mesma limpeza roda em dobro | `pg_try_advisory_lock` por tarefa: quem não pega o lock pula a rodada. Apaga em lotes com `LIMIT` | 13, E2 |
| **Limpeza apaga uma sandbox em uso** | Uma conversa em andamento perde os dados | Só apaga o que passou de 24 h **e** não tem lease ativa | E2 |

**O banco é a última barreira.** Mesmo com a lógica certa, as restrições ficam no esquema: chave de idempotência
única, índice único parcial das aprovações, `CHECK (amount_cents > 0)`, chaves estrangeiras e o trigger do
`audit_log`. Se o código errar, o banco recusa.

## 2. Idempotência

Fazer a mesma operação duas vezes tem de dar o mesmo resultado que fazer uma vez. Isso vale em três lugares:

| Onde | Como |
| --- | --- |
| **Ações com efeito** (reembolso, cupom, cancelamento) | Chave de idempotência derivada da conversa e da proposta, com restrição única. A segunda execução encontra a primeira e devolve o mesmo resultado (decidido no tema 2) |
| **Mensagem do cliente** (rede instável, o cliente reenvia) | A interface manda um `client_message_id` (UUID) em cada mensagem; há uma restrição única por conversa. Repetida → a API devolve o turno que já existe, sem rodar o grafo de novo |
| **Retomada da aprovação** | O nó `await_approval` não tem efeito colateral e o `execute_action` é idempotente: retomar duas vezes gera uma ação (cenário F2) |
| **Evento para fora** (webhook do n8n) | *Outbox*: o evento é gravado na mesma transação da ação e enviado depois, com novas tentativas e um `event_id` para o receptor descartar duplicatas | E7 |

**Uma camada só de novas tentativas** (change 5): as dos SDKs de LLM ficam desligadas e o `with_retry` repete só erro
passageiro, de modo que o orçamento enxerga cada pedido que sai. O `invoke` síncrono de um modelo é proibido em
`src/`, porque engole a recusa do orçamento.

**Nova tentativa só em operação idempotente.** Chamada de LLM, leitura e envio com `event_id` podem ser
repetidos; uma escrita sem chave de idempotência, nunca. Toda nova tentativa tem **número máximo** e **espera
exponencial com jitter**, e respeita o `Retry-After` de quem respondeu; tentar de novo em laço apertado só piora
um serviço que já está sobrecarregado.

## 3. Limites, timeouts e carga

| Limite | Valor inicial | Ao estourar | Change |
| --- | --- | --- | --- |
| Requisições por sessão e por IP nas rotas de conversa e de sandbox | Janela deslizante em memória | `429 rate_limited` com `Retry-After` | 13 |
| Turnos executando ao mesmo tempo (semáforo) | 8 | `503 busy` com `Retry-After` | 13 |
| Conexões SSE abertas | 50 | `503 busy` | 13 |
| Tamanho da mensagem e do corpo | 1.000 caracteres / 16 KB | `422` / `413` | 13 |
| Requisições por minuto a cada provedor de LLM | Abaixo do limite gratuito (`InMemoryRateLimiter`, metade como rajada e metade como reposição) | Espera; um `429` do provedor respeita o `Retry-After` dele | 5 |
| Orçamento diário por provedor | Dia em **UTC**. Padrões em 80% da cota conferida na conta em 08/11: Gemini 400 requisições (de 500) e 2.000.000 de tokens, Groq 800 (de 1.000) e 150.000 (de 200.000), Ollama sem limite, Anthropic zero (nenhuma chamada real por engano) | Reserva, depois modo degradado | 5 |
| Pausa do provedor depois de um `429` | `Retry-After` ou `retryDelay` do Gemini; 5 s se ilegível, negativo ou data HTTP; teto de 1 h. Até 2 s restantes a chamada espera; acima disso é recusada na hora e vai para a reserva | Reserva, depois modo degradado | 5 |
| Prazo total de uma chamada de LLM (todas as tentativas e os dois provedores) | 25 s no `writer`, 45 s no `agent`, 90 s nos demais | Modo degradado (`deadline`) | 5 |
| Execuções de LLM abertas no registro do governor | 1.024, a mais antiga descartada (cancelamento não dispara `on_llm_error`) | Uma chamada cancelada não gera linha, e a reserva fica contada | 5 |
| Chamadas de ferramenta, revisões e turnos | 8 / 2 / 15 | Transferência para humano | 12 |
| Prazo total de um turno | 60 s | Modo degradado e transferência | 12 |
| Timeout de LLM por tentativa e por papel | 20 s no agente, 10 s no `writer`, 30 s nos demais | Nova tentativa, reserva, modo degradado | 5 |
| Timeout de consulta no banco (`statement_timeout`) | 5 s | Erro tratado, nunca uma ação parcial | 2 |
| Pool de conexões | 10 + 5 de folga, `pool_timeout` de 5 s | `503 busy` | 2 |
| Listagens (fila de aprovações, conversas) | Paginação por cursor, no máximo 50 itens | — | 13 |

**Os limites em memória valem para uma instância só.** Isso está escrito de propósito: com mais de uma, os
contadores iriam para o Postgres ou para um Redis. O que já usa o banco (lease, orçamento, sandboxes) funciona com
várias instâncias sem mudança.

## 4. Falhas no meio do caminho

| Situação | Decisão | Change |
| --- | --- | --- |
| **O cliente fecha a aba no meio do turno** | O turno **não depende da conexão**: roda numa tarefa da aplicação (no `TaskGroup` do `lifespan`), protegida pela lease, e o SSE só transmite os eventos. Ao voltar, o cliente lê o resultado em `GET /conversations/{id}` | 13 |
| **A aplicação reinicia no meio do turno** | O checkpoint (durabilidade `sync`) guarda o último passo; a ação ou foi gravada inteira, na transação, ou não foi. A lease expira e a conversa volta a aceitar mensagens | 12, 13 |
| **Desligamento** | O `lifespan` para de aceitar turnos novos e espera os que estão rodando por até 20 s; o resto fica com a lease expirando | 13 |
| **Transação longa** | Nenhuma chamada de LLM ou de rede dentro de uma transação do banco. Transação só em volta de escrita curta | 2 em diante |
| **Cancelamento do asyncio** (`CancelledError`) no meio de uma escrita | A escrita é uma transação só; cancelada, ela é desfeita. Nunca se captura `CancelledError` sem relançar | 2 em diante |

## 5. Observabilidade desde a primeira change

Observabilidade não é a change 14: ela só liga os traces ao Langfuse. **Cada change registra o que cria.**

- **Logs com structlog a partir da change 2,** com o nome do evento no formato `<módulo>.<evento>`
  (`policy.denied`, `actions.executed`, `api.turn_conflict`). Sem dado pessoal, com o filtro de mascaramento.
- **Toda decisão do sistema deixa rastro:** a política negou, o antifraude mandou para humano, a lease estava
  ocupada, a reavaliação negou na execução, o limite de requisições barrou, o orçamento acabou, o provedor caiu e a
  reserva assumiu.
- **Esses eventos viram métricas do painel** (`observabilidade.md`, seção 4): conflitos de turno, `429`, `503`,
  negativas na reavaliação, modo degradado. Se eles crescem, alguma coisa está errada antes de um cliente reclamar.
- **A change 14** adiciona o `trace_id` em todo log e nas tabelas, os spans e o Langfuse.

## 6. Entrada não confiável

Todo texto que vem de fora (mensagem do cliente, observação do pedido, saída do LLM, resposta de um provedor) é
dado, nunca instrução nem código.

| Risco | Decisão | Change |
| --- | --- | --- |
| **Unicode enganoso:** dígitos de largura total (`１２３`), caracteres invisíveis, letras de outro alfabeto, para escapar do mascaramento ou da rede de palavras-chave | Remoção dos caracteres de formatação invisíveis, NFKC e conversão de dígitos de qualquer alfabeto e de variações de hífen, **antes** do mascaramento e da classificação (`privacy.normalize_text`); o texto original nunca é guardado. Letras parecidas de outro alfabeto ficam como risco residual, porque o NFKC não as converte | 4 |
| **Expressão regular que trava** (ReDoS) com uma entrada feita para isso | Regex com repetição limitada, testadas com entradas longas e repetitivas (menos de 50 ms em 1.000 caracteres); a mensagem já tem no máximo 1.000 caracteres, e o mascaramento omite o que passa de 64.000, antes ou depois do NFKC | 4 |
| **Injeção em log** (quebra de linha falsa num campo) | Logs em JSON pelo structlog, que escapa os valores; texto do cliente nunca vai para a mensagem do log, só mascarado e como campo | 2 |
| **Saída do LLM** usada como dado | Validada pelo Pydantic; nunca vira SQL, comando, URL nem HTML (`seguranca.md`, LLM05) | 5, 12 |
| **SSRF** (o servidor buscar uma URL escolhida por alguém) | Nenhuma rota nem ferramenta busca URL vinda do cliente ou do LLM. As saídas de rede são só para destinos fixos na configuração (provedores de LLM, Hugging Face, Langfuse) | Todas |
| **Webhook recebido** (se o n8n chamar a API) | Assinatura HMAC com segredo e carimbo de tempo; requisição velha ou sem assinatura é recusada | E7 |

## 7. Evolução sem quebrar o que está no ar

| O que muda | Risco | Decisão |
| --- | --- | --- |
| **Estado do grafo e nós do LangGraph** | Uma conversa pausada esperando aprovação é retomada com o código **novo**. Renomear ou remover um nó, mudar o `interrupt` ou tornar um campo do estado obrigatório quebra essa retomada ([docs](https://docs.langchain.com/oss/python/langgraph/backward-compatibility)) | Mudança no estado só aditiva, com valor padrão; nó não se renomeia nem se remove enquanto houver aprovação pendente. Antes de um deploy que mexa no grafo, as aprovações pendentes são decididas ou expiram. Um teste de integração retoma, com o grafo atual, um checkpoint gravado pela versão anterior |
| **Esquema do banco** | Migration que quebra a versão que ainda está rodando | Expandir e depois contrair: coluna nova anulável ou com padrão; remoção só numa migration seguinte. Toda migration tem `downgrade` testado |
| **API** | Interface antiga quebrada | Rotas em `/v1`; campo novo é opcional; remover ou mudar significado exige `/v2` |
| **Prompt, modelo e dataset** | Resultado muda sem ninguém saber por quê | Versão registrada em cada chamada e em cada relatório (`arquitetura.md`, `ml-e-avaliacao.md`) |

## 8. Dados, consultas e configuração

- **Índice para todo filtro usado em rota ou ferramenta**, a começar por `sandbox_id` e `customer_id`, criado na
  mesma migration da consulta.
- **Sem N+1:** listagens carregam o que precisam numa consulta (`selectinload` ou JOIN); um teste de integração
  conta as consultas das rotas de listagem.
- **Arredondamento de dinheiro** só no `Money`, com regra escrita (meio para cima, em centavos); reembolso parcial
  nunca soma mais que o valor pago (`CHECK` e teste de propriedade).
- **Falhar cedo na subida:** `Settings` valida tudo ao iniciar; segredo faltando, modelo com esquema errado ou banco
  sem migration → a aplicação não sobe, com mensagem clara, em vez de falhar no primeiro cliente.
- **Padrões seguros:** modo de depuração desligado, CORS fechado, `/docs` sem dados reais; o que é perigoso exige
  variável explícita.
- **Testes determinísticos:** semente fixa, relógio falso, nada de `sleep` para esperar algo acontecer. Teste
  intermitente é bug, corrigido ou removido, nunca repetido até passar.
- **Cópia de segurança:** os dados da demo são sintéticos e recriáveis pelo seed; a cópia de segurança é o próprio
  gerador. Dado que não se recria (rótulos humanos, teste escrito por pessoas) fica versionado no Hugging Face.

### Desempenho e travas no banco

Os dados da demo são pequenos, mas as consultas são escritas como se a tabela tivesse milhões de linhas: é o
hábito que importa, e o custo é o mesmo.

| Assunto | Regra |
| --- | --- |
| **Plano da consulta** | Consulta nova de rota ou ferramenta tem o plano conferido com `EXPLAIN (ANALYZE, BUFFERS)` sobre os dados do seed; `Seq Scan` numa tabela que cresce indica índice faltando. O design registra o índice escolhido |
| **Chave estrangeira** | O Postgres **não** cria índice para chave estrangeira. Toda FK usada em filtro, JOIN ou `ON DELETE` tem índice na mesma migration |
| **Índice composto** | Colunas de igualdade primeiro, depois a de intervalo ou ordenação: `(sandbox_id, customer_id, created_at)`. Índice parcial para o subconjunto quente (`WHERE status = 'pending'`) |
| **Listagem** | Sempre com `LIMIT`. Paginação por cursor (*keyset*: `WHERE (created_at, id) < (:c, :i) ORDER BY created_at DESC, id DESC`), nunca `OFFSET`, que lê e descarta tudo antes da página. Nada de `count(*)` só para saber se há mais página |
| **Colunas** | Listagem seleciona só o que mostra; nunca carrega `embedding` ou texto longo para montar uma lista |
| **Carga de relacionamentos** | No SQLAlchemy assíncrono, carga preguiçosa quebra (`MissingGreenlet`) e esconde N+1. Relacionamentos com `lazy="raise"`, carga explícita (`selectinload`) e sessão com `expire_on_commit=False` |
| **Escrita em massa** | Seed, gerador e tarefas de fundo inserem e apagam em lotes (`insert()` com várias linhas, `COPY` no seed grande), nunca uma linha por vez num laço com `commit` |
| **Travas** | Ordem fixa entre tabelas, registrada no design (por exemplo: conversa → pedido → ação), para não haver *deadlock*. Transação que trava define `SET LOCAL lock_timeout` curto (2 s); estourou → `ConflictError`, que o chamador pode repetir porque a operação é idempotente |
| **Isolamento** | `READ COMMITTED`, o padrão. Corrida se resolve com trava explícita, UPDATE condicional ou restrição, nunca contando com o nível de isolamento |
| **Migrations** | `lock_timeout` no início de cada migration, para não travar a aplicação esperando uma trava. Índice em tabela que já tem dados é criado com `CONCURRENTLY` (fora de transação, no `autocommit_block` do Alembic). Migração de dados em lotes, separada da migração de esquema |
| **Pool** | A soma dos pools de todos os processos (API, tarefas, migrations) cabe no limite de conexões do banco da demo |

## 9. Serviços de fora

| Serviço | Se cair ou ficar lento |
| --- | --- |
| Gemini | Até 2 novas tentativas, depois o Groq, depois o modo degradado (`arquitetura.md`). Num `429`, o `ProviderGate` pausa o provedor |
| Groq | Modo degradado |
| Langfuse | Exportação em lote e assíncrona; uma falha é descartada e registrada, **nunca** atrasa nem derruba uma conversa |
| Hugging Face (modelos) | Só usado no build da imagem; em execução a aplicação não depende dele |
| Neon (banco da demo) | O primeiro acesso depois de inativo pode demorar cerca de um segundo; `/ready` e o `pool_pre_ping` cobrem |

Duas armadilhas dos provedores compatíveis com a OpenAI (Groq e Ollama):

- **Um cliente HTTP por processo.** O `langchain_openai` guarda um único cliente assíncrono por processo, preso
  ao primeiro event loop que o usou. A API roda num loop só e não é afetada. Já um script que abre um loop por
  caso (`asyncio.run` dentro de um laço, por exemplo no runner de evals) recebe `Event loop is closed` a partir
  do segundo caso. Por isso, um script deve rodar todos os casos num único loop, e os testes `llm` compartilham
  o loop do módulo.
- **Tokens por minuto.** O Groq limita a 8.000 tokens por minuto por modelo, e o nosso limitador só conta
  requisições por minuto. Uma conversa longa com ferramentas pode passar desse teto. O `429` que vem nesse caso
  cai na pausa e na reserva, como qualquer falha; um teto próprio de tokens por minuto fica para quando os evals
  mostrarem que ele faz falta.

## 10. Como se testa

Corrida não aparece em teste sequencial. Para cada linha da seção 1 há um **teste de integração que dispara as
operações juntas** (`asyncio.gather` contra o Postgres do Testcontainers) e confere o estado final: um reembolso,
uma decisão, um turno, o teto respeitado. Os limites da seção 3 têm teste com o relógio falso; as falhas da seção 4
têm teste com o LLM falso travando ou levantando erro no meio; a seção 7 tem o teste de retomada de checkpoint
antigo.

## 11. Checklist do design de cada change

- [ ] Toda rota ou ferramenta nova está na matriz de `autorizacao.md` e no teste que percorre as rotas? Toda
  consulta nova recebe o `AccessScope`?

- [ ] Alguma leitura seguida de escrita pode correr em paralelo? Qual trava, restrição ou UPDATE condicional
  impede a corrida, e qual teste dispara as duas juntas?
- [ ] Alguma operação pode ser repetida (reenvio, nova tentativa, retomada)? Qual a chave de idempotência?
- [ ] Toda chamada externa tem timeout, e as novas tentativas são só em operação idempotente, limitadas e com
  espera exponencial e jitter?
- [ ] Algum recurso pode ser esgotado (cota, conexões, memória, tarefas)? Qual o limite e o que acontece ao estourar?
- [ ] O que acontece se o processo cair ou a tarefa for cancelada no meio?
- [ ] Alguma transação envolve rede ou LLM?
- [ ] Que eventos de log a change cria, e algum deles vira métrica?
- [ ] Algum texto de fora (cliente, LLM, provedor) é usado sem normalizar e validar? Alguma regex nova roda sobre ele?
- [ ] A change mexe no estado ou nos nós do grafo, no esquema do banco ou na API? A mudança é aditiva e compatível
  com o que está pausado ou rodando?
- [ ] Toda consulta nova tem índice (conferido com `EXPLAIN`), `LIMIT` e paginação por cursor, e não faz N+1?
  Toda chave estrangeira nova tem índice?
- [ ] As travas seguem a ordem combinada e têm `lock_timeout`? Alguma migration pode travar uma tabela com dados?
- [ ] Alguma configuração nova pode faltar? A aplicação falha na subida, com mensagem clara?
