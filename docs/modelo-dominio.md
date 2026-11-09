# Modelo de domínio, política e dados

Tema 4 da etapa de arquitetura, decidido em 2026-10-31. Define as entidades e tabelas, a política do Deliverius
(números e documentos), o gerador de dados sintéticos e os conjuntos de dados. A busca nas políticas (RAG) está em
`arquitetura.md`, seção "Busca nas políticas".

## 1. Convenções de dados

| Assunto | Decisão | Por quê |
| --- | --- | --- |
| Dinheiro | Inteiro em **centavos** (`amount_cents`), embrulhado num tipo `Money` em `shared/` | `float` erra centavos (0,1 + 0,2 ≠ 0,3). Reembolso errado por arredondamento é bug de dinheiro |
| IDs | **UUID v7** gerado na aplicação | Ordenado pelo tempo (bom para índice), não revela quantos pedidos existem e pode ser gerado antes de ir ao banco (útil para idempotência). A biblioteca que gera depende da versão do Python, decidida no tema 6 |
| Tempo | `timestamptz` em UTC no banco; `America/Sao_Paulo` só na exibição; **relógio injetado** (`Clock` em `shared/`) | O gerador simula meses de pedidos, e os testes precisam controlar o "agora" (prazos da política) |
| Texto livre do cliente | Guardado **só mascarado** | Minimização (LGPD): o texto original nunca é gravado |
| Auditoria | Tabela **só de inserção**; um trigger no Postgres recusa `UPDATE` e `DELETE` | Quem aprovou o quê não pode ser reescrito, nem por bug |
| Enum | Python `StrEnum` e `text` com `CHECK` no banco | Legível nas consultas e simples de migrar |

## 2. Entidades e tabelas

```
customers ─┬─< orders >── restaurants
           │     ├─< order_items
           │     └─< delivery_events
           │
           └─< conversations ─┬─< messages            (texto mascarado, intenção, confiança)
                              ├─< actions             (reembolso, cupom, cancelamento; idempotency_key UNIQUE)
                              ├─< approval_requests   (proposta, decisão da política, nota e fatores do antifraude)
                              ├─< handoffs            (resumo estruturado, prioridade)
                              └─< llm_calls           (papel, modelo, prompt, tokens, custo, latência)
                                  (a ligação com `conversations` entra na change 13, como `NOT VALID`)

audit_log            (só inserção: quem, o quê, quando, ID de correlação)
policy_chunks        (ID citável, documento, seção, texto preenchido, hash, embedding de 384, tsvector gerado)
langgraph checkpoints (tabelas do AsyncPostgresSaver; detalhe interno do LangGraph)
```

| Tabela | Campos principais | Observações |
| --- | --- | --- |
| `customers` | nome, CPF, telefone, e-mail, endereço (todos sintéticos), criado em | Os dados pessoais existem para testar o mascaramento. Nunca vão para LLM nem para log |
| `restaurants` | nome, categoria | — |
| `orders` | sandbox, cliente, restaurante, status, feito em, entregue em, previsão, subtotal, taxa, desconto, total, forma de pagamento, **observação** | A observação é texto livre: é o vetor do prompt injection indireto (cenário F4b). Uma FK composta `(customer_id, sandbox_id)` impede o pedido de apontar para cliente de outra sandbox; `CHECK` garante `total = subtotal + taxa − desconto` |
| `order_items` | nome, preço unitário, quantidade, alérgenos | — |
| `delivery_events` | **status** alcançado (o mesmo `OrderStatus` do pedido), quando, por quem (`customer`, `restaurant`, `courier`, `system`) | A linha do tempo que o agente consulta. O status do pedido é o último da linha do tempo |
| `conversations` | cliente, pedido, status (aberta, aguardando aprovação, transferida, encerrada) | `id` = `thread_id` do LangGraph |
| `messages` | papel, texto mascarado, intenção, confiança | — |
| `actions` | tipo, valor, itens, status, **chave de idempotência (única)**, executada por (automático ou aprovador), quando | A chave é derivada da conversa mais a proposta, então repetir a execução não duplica |
| `approval_requests` | proposta, decisão da política, nota de fraude e fatores, status, quem decidiu, motivo, **versão** | A versão impede duas decisões concorrentes (trava otimista) |
| `handoffs` | prioridade, resumo (intenção, fatos, ações já feitas), status | — |
| `llm_calls` | `id`, `created_at`, `conversation_id` e `sandbox_id` (anuláveis), `role`, `provider`, `model`, `prompt_name`, `prompt_version` e `prompt_hash` (anuláveis), `status` (`ok` ou `error`), `error_type`, `latency_ms`, tokens de entrada, de saída e de raciocínio, custo real e custo equivalente (em micros de dólar), `trace_id` | Uma linha por tentativa real, com sucesso ou erro. **Nunca guarda texto de mensagem nem texto de erro do provedor**, só o tipo da exceção. `NULL` não é zero: token ausente e preço desconhecido ficam vazios, para a soma do painel não mentir. O custo é inteiro em **micros de dólar** (uma chamada custa frações de centavo); os tokens de raciocínio já estão em `output_tokens` e não somam de novo. `CHECK` recusa `status` desconhecido e valores negativos. Índices: `created_at` (retenção e painel) e `(conversation_id, created_at)` parcial onde `conversation_id IS NOT NULL` (custo por conversa). Sem `AccessScope` por ora: só há escritas de sistema, e a leitura entra no E1 |
| `llm_budget_usage` | `provider` e `day` (chave primária), `requests_used`, `tokens_used` | O contador do orçamento diário, com o dia em **UTC**. Mexe-se nele só por duas instruções únicas: a reserva (`INSERT … ON CONFLICT DO UPDATE … WHERE … RETURNING`, que recusa sem linha de volta) e a soma de tokens (`SET tokens_used = tokens_used + :n`). `CHECK` recusa contadores negativos. A retenção das linhas antigas entra na change 13 |
| `audit_log` | sandbox, tipo e ID do ator, ação (`<módulo>.<evento>`), tipo e ID do alvo, dados, quando, ID de correlação (`trace_id`) | Só inserção: triggers recusam `UPDATE`, `DELETE` e `TRUNCATE`. Os dados nunca levam dado pessoal |

**Status, linha do tempo e pagamento** (decididos na change `nucleo-dominio`):

```
placed → accepted → preparing → ready_for_pickup → out_for_delivery → delivered
   └──────────┴──────────┴──────────────┴──→ cancelled         (só antes de sair para entrega)
                                         out_for_delivery → delivery_failed
```

- **Status do pedido (`OrderStatus`):** `placed`, `accepted`, `preparing`, `ready_for_pickup`, `out_for_delivery`,
  `delivered`, `cancelled`, `delivery_failed`. Transições em frente, um passo por vez; cancelamento só antes de
  sair para entrega; falha só depois. A tabela de transições fica em `orders/models.py` (`ALLOWED_TRANSITIONS`).
- **Eventos da entrega usam o mesmo vocabulário** (`status`, não `type`): cada evento é a chegada a um status.
  Atraso não é evento; é calculado da previsão contra o instante real.
- **Formas de pagamento (`PaymentMethod`):** `pix`, `credit_card`, `debit_card`, `meal_voucher`. Só pagamento
  pelo app, então todo estorno volta pelo mesmo meio. Dinheiro na entrega fica fora.
- **Restaurantes são um catálogo comum** a todas as sandboxes (sem dado pessoal, nenhuma rota cria restaurante).
- Valores novos entram por migration que troca o `CHECK`; nenhum valor é removido.

**Reembolso e cupom são tipos de `actions`, não tabelas separadas.** Os dois têm o mesmo ciclo (proposta →
política → aprovação → execução), e a auditoria e a idempotência ficam num lugar só.

## 3. A política do Deliverius

A política é inventada para o projeto, mas com números plausíveis. **Os valores são provisórios** e serão
ajustados depois de ver os dados gerados e o custo dos erros do antifraude (tema 5).

### Fonte única dos números

Os números ficam num arquivo de configuração tipado (`policy/limits.py`, `PolicyLimits`), que **o motor de política
usa**. Os documentos em markdown têm marcadores com o nome de cada campo (`{{auto_approval_limit}}`), que
`render_policy_document` **preenche com esses números**; a indexação (change 8) indexa o texto já preenchido. Um
teste garante que nenhum documento escreve um valor em reais ou um prazo à mão.

Assim o texto que o RAG cita e o código que decide nunca divergem. Se a política disser "até R$ 50" e o código
permitir R$ 80, o cliente recebe uma promessa que o sistema não cumpre.

### Regras

| Situação | Resolução | Teto | Prazo para reclamar |
| --- | --- | --- | --- |
| Item faltando | Reembolso do item | Valor do item | 24 h após a entrega |
| Item errado | Reembolso do item | Valor do item | 24 h |
| Qualidade (frio, derramado, violado) | Cupom ou reembolso parcial | 50% do item | 24 h |
| Atraso de 30 a 60 min além da previsão | Cupom | 10% do pedido, máximo R$ 15 | 24 h |
| Atraso de mais de 60 min | Cupom de R$ 20 ou cancelamento com reembolso total, se ainda não saiu para entrega | Total | Durante o pedido ou até 24 h |
| Não entregue (falha na entrega) | Reembolso total | Total | 48 h |
| Não entregue, mas marcado como entregue | **Sempre análise humana** | Total | 48 h |
| Cobrança duplicada | Estorno da duplicata, **sempre com análise humana** | Total | 90 dias |
| Cancelamento antes de o restaurante aceitar | Grátis, reembolso total | Total | — |
| Cancelamento depois de aceito | **Análise humana** (o revisor consulta o restaurante) | Total | Antes de sair para entrega |
| Alergia, segurança alimentar, mal-estar | **Sempre humano, prioridade alta** | — | — |
| Conduta do entregador, assédio, acidente | **Sempre humano, prioridade alta** | — | — |

Os tetos de "Total" são sempre o total do pedido **ainda não reembolsado** (reembolsos e cancelamentos já
executados no pedido). O motor decide em seis etapas fixas: forma da proposta, estado e prazo, o que já foi
reclamado, teto, casos sempre humanos e limites de autoaprovação. Passar do **teto** nega a proposta (o agente
revisa); passar de um **limite** manda para um humano.

**De onde conta cada prazo** (o instante da reclamação vem de quem chama; o motor não lê o relógio):

| Situação | Estado exigido | O prazo conta a partir de |
| --- | --- | --- |
| Item faltando, item errado, qualidade | Entregue | `delivered_at` |
| Atraso | Qualquer um, exceto cancelado ou falha na entrega | `delivered_at`; sem prazo enquanto o pedido está em andamento |
| Não entregue | Falha na entrega, ou marcado como entregue | O evento `delivery_failed`, ou `delivered_at` |
| Cobrança duplicada | Qualquer um | `placed_at` |
| Cancelamento | Antes de sair para entrega | — |

O atraso é medido da previsão até a entrega, ou até o instante da reclamação se o pedido ainda não chegou.

**Pontos decididos no motor** (change `motor-politica`):

| # | Ponto | Regra |
| --- | --- | --- |
| R1 | Cancelamento antes do aceite e os limites | Isento dos limites por ação e de 30 dias: não há incidente a verificar nem comida feita |
| R2 | Cobrança duplicada "confirmada" | Sempre humano: o sistema não tem o dado da cobrança para confirmá-la |
| R3 | O que conta nos R$ 100 em 30 dias | Reembolsos, cupons e cancelamentos por atraso **autoaprovados**, de qualquer pedido. Não contam os aprovados por um revisor nem os cancelamentos pedidos pelo cliente |
| R4 | O que é "não entregue" | Falha na entrega (reembolso) ou marcado como entregue (humano). Pedido ainda a caminho segue a regra de atraso |
| R5 | Base dos 10% do atraso | O total do pedido, com taxa e desconto |
| R6 | Item reclamado duas vezes, por motivos diferentes | Bloqueado: a unidade que já recebeu reembolso ou cupom conta como reclamada |
| R7 | Cancelamento depois do aceite | Precisa de aprovação (`restaurant_already_accepted`): o revisor consulta o restaurante e decide no mesmo fluxo |
| R8 | Teto × limite de autoaprovação | Acima do teto → negada; acima do limite → precisa de aprovação |
| R9 | Atraso compensado mais de uma vez | Uma compensação por pedido: depois de um cupom ou cancelamento por atraso, outra é negada (`delay_already_compensated`) |

### Limites de autoaprovação (acima deles → aprovação humana)

| Limite | Valor |
| --- | --- |
| Valor por ação | R$ 50 |
| Valor autoaprovado por cliente em 30 dias | R$ 100 |
| Cupom por ação | R$ 20 |
| Nota de fraude | Limiar escolhido pelo custo esperado do erro (tema 5) |

### Documentos (`src/deliverius/policy/docs/`, indexados para o RAG)

| ID | Documento |
| --- | --- |
| POL-01 | Cancelamento |
| POL-02 | Atrasos |
| POL-03 | Item faltando |
| POL-04 | Item errado |
| POL-05 | Qualidade do pedido |
| POL-06 | Pedido não entregue |
| POL-07 | Cobranças e estornos |
| POL-08 | Cupons |
| POL-09 | Segurança alimentar e alergias |
| POL-10 | Conduta e segurança na entrega |
| POL-11 | Reembolsos: prazos e formas de pagamento |
| POL-12 | Privacidade e dados pessoais |

## 4. Gerador de dados sintéticos (`datagen/`)

A ideia central, já decidida no plano: **o cenário vem primeiro e o rótulo é verdade de construção.** O LLM nunca
decide um rótulo; ele só escreve o texto do cliente.

### Camadas

1. **Simulador do mundo** (código, com semente fixa, sem LLM):
   - clientes com um **perfil de comportamento**: honesto (≈ 90%), oportunista (≈ 7%), fraudador (≈ 3%);
   - restaurantes;
   - seis meses de pedidos e linhas do tempo de entrega, com incidentes sorteados a taxas realistas;
   - dados pessoais falsos com `Faker` pt-BR (CPF com dígito válido), para testar o mascaramento.
2. **Reclamações e rótulos de fraude:**
   - um honesto reclama de incidentes reais;
   - um fraudador inventa incidentes;
   - um oportunista exagera.

   O rótulo `fraude` diz se a reclamação **não tem incidente real** por trás. Para não ficar trivial:
   - honestos às vezes têm muitos incidentes legítimos seguidos;
   - fraudadores às vezes têm incidentes reais;
   - 2% a 3% dos rótulos são trocados de propósito.
3. **Resolução correta:** o **motor de política** calcula a ação e o valor certos para cada reclamação. É a mesma
   função que a aplicação usa, então o gabarito dos evals vem da regra de verdade.
4. **Mensagens (LLM, papel `datagen`; o código decide tudo menos o texto):**
   - a partir do cenário, o LLM escreve como um cliente escreveria: persona, tom, gíria, erro de digitação,
     mensagem imprecisa;
   - uma parte das mensagens traz dados pessoais, para testar o mascaramento, e uma parte traz tentativas de
     prompt injection;
   - o rótulo de intenção vem do cenário.
5. **Cenários de ponta a ponta:** o estado inicial do banco, o objetivo do cliente e o **estado final esperado**
   (estilo τ-bench), usados pelo cliente simulado nos evals.

### Marcadores e rótulos das mensagens

O LLM escreve variações com marcadores (`[[NOME]]`, `[[CPF]]`, `[[TELEFONE]]`, `[[EMAIL]]`, `[[ENDERECO]]`,
`[[INJECAO]]`) e o código os troca por um valor do Faker ou por um ataque de um catálogo fixo de 30 ataques, gravando
o tipo e a posição de cada dado pessoal. Assim nenhum dado pessoal, nem falso, vai ao LLM, e os rótulos de intenção,
de privacidade e de injeção são verdade de construção. Variação com marcador desconhecido, não pedido ou ausente,
vazia, acima de 600 caracteres ou repetida é descartada e contada no manifesto. Cada chamada pede 10 variações de um
cenário, com saída estruturada: cerca de 345 chamadas ao Gemini por geração completa, dentro da cota de um dia.

**Rótulo de fraude:** a reclamação pede algo sem incidente real por trás (inventada inteira, ou o exagero do
oportunista). O perfil do cliente, a marca de rótulo trocado e o desfecho do revisor humano ficam fora do conjunto de
treino do antifraude, porque entregariam o rótulo.

### Intenções (11 classes)

`item_missing`, `wrong_item`, `quality_issue`, `late_delivery`, `not_delivered`, `billing_issue`,
`cancel_order`, `coupon_question`, `policy_question`, `sensitive` (alergia, saúde, segurança, conduta) e
`other` (saudação, fora de escopo).

### Conjuntos e tamanhos

| Conjunto | Tamanho | Uso | Divisão |
| --- | --- | --- | --- |
| Mensagens rotuladas (treino, validação e teste) | ≈ 3.000 (297 cenários × 10 variações) | Classificador | **Por cenário**, 70/15/15, estratificado por intenção: variações do mesmo cenário nunca ficam em lados diferentes, senão o teste mede memória. A parte de teste é o "teste sintético do mesmo gerador" de `ml-e-avaliacao.md` |
| Teste sintético de outro gerador | ≈ 550 (55 cenários) | Classificador | Gerado por um modelo de outra família (Groq, papel `datagen_holdout`), com prompt próprio e cenários disjuntos dos do conjunto principal |
| **Teste escrito por pessoas** | ≈ 150 | Classificador | Nunca usado em treino nem em ajuste |
| Histórico de pedidos e reclamações | ≈ 20.000 pedidos | Antifraude | **Por tempo**: treino nos primeiros meses, teste no último |
| Pares pergunta → trecho da política | ≈ 60 | Avaliação do RAG | — |
| Cenários de ponta a ponta | ≈ 150 gerados | Evals | O tamanho rodado por vez é definido no tema 7, por causa da cota |

### Versionamento

- **O gerador é reprodutível:** mesma versão e mesma semente produzem os mesmos dados.
- **Os conjuntos completos vão para o Hugging Face Datasets**, com versão e *dataset card*. O card explica que os
  dados são sintéticos, como foram gerados e quais são os limites.
- **Só amostras pequenas ficam em `data/`**, no git.
- **Cada versão traz um `manifest.json`:** versão do gerador, semente, commit, instante de referência, versões e
  hashes dos prompts, modelos por papel, contagens, descartes, corte temporal, ruído, taxa de fraude e o SHA-256 de
  cada arquivo. A leitura recusa arquivo cujo hash não bate. Mudou a saída, sobe `DATAGEN_VERSION` (um teste de hash
  dourado obriga).
- **Reprodutível pelo cache na camada do LLM:** as camadas 1, 2, 3 e 5 dependem só da semente; as mensagens, do
  arquivo de cache de respostas (o Flash-Lite ignora a temperatura). O conjunto publicado é a referência.
- O antifraude também vai para o Kaggle (extra da F6).

## 5. Alternativas consideradas

| Decisão | Alternativas | Por que esta |
| --- | --- | --- |
| Dinheiro em centavos | `float`, `Decimal`, `NUMERIC` | Inteiro é exato, rápido e trivial de serializar. `Decimal` também é exato, mas é mais fácil de usar errado misturado com `float` |
| Números da política em código, texto com marcadores | Números escritos à mão nos dois lugares | Fonte única: o texto citado nunca contradiz a regra executada |
| Ações numa tabela só | Uma tabela por tipo (reembolso, cupom) | Mesmo ciclo de vida, uma auditoria e uma idempotência |
| Divisão por cenário no classificador | Divisão aleatória por mensagem | A aleatória põe paráfrases do mesmo cenário no treino e no teste e infla a nota |
| Divisão por tempo no antifraude | Aleatória | Em produção o modelo prevê o futuro com dados do passado. A aleatória vaza informação do futuro |
