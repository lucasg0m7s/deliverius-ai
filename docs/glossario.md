# Glossário

Termos do domínio em português, como aparecem na documentação e para o cliente, e o nome usado no código, sempre
em inglês. Quando um termo novo surgir, ele entra aqui antes de entrar no código.

| Português | Código | Significado |
| --- | --- | --- |
| Deliverius | `deliverius` | O app de delivery fictício |
| Deli | `deli` (só em texto) | O agente de atendimento com IA |
| Cliente | `customer` | Quem fez o pedido e conversa com o Deli |
| Pedido | `order` | — |
| Item do pedido | `order_item` | — |
| Restaurante | `restaurant` | — |
| Resumo do pedido | `OrderSummary` | O que a lista de pedidos mostra: sem itens e sem observação |
| Status do pedido | `OrderStatus` | `placed`, `accepted`, `preparing`, `ready_for_pickup`, `out_for_delivery`, `delivered`, `cancelled` ou `delivery_failed`. As transições válidas ficam em `ALLOWED_TRANSITIONS` |
| Linha do tempo da entrega | `delivery_events` (`DeliveryEvent`) | Cada evento é a chegada a um `OrderStatus`, com instante e quem causou. Atraso não é evento: é calculado da previsão contra o instante real |
| Quem causou o evento | `EventActor` | `customer`, `restaurant`, `courier` ou `system` |
| Forma de pagamento | `PaymentMethod` | Só pelo app: `pix`, `credit_card`, `debit_card` ou `meal_voucher` |
| Dinheiro | `Money` | Valor em centavos inteiros; nunca negativo; percentual arredondado metade para cima |
| Sandbox | `sandbox_id` | O espaço isolado de dados de um visitante da demo; ninguém enxerga a sandbox de outro |
| Escopo de acesso | `AccessScope` | O que uma leitura pode ver: `CustomerScope` (um cliente numa sandbox) ou `SandboxScope` (a sandbox inteira, para o revisor e tarefas internas). Obrigatório em todo repositório |
| Página e cursor | `PageRequest`, `Page` | Listagem do mais recente para o mais antigo, no máximo 50 por página, com cursor opaco para a próxima |
| Entrada de auditoria | `AuditEntry` (`audit_log`) | Quem (`actor_type`, `actor_id`) fez o quê (`action`, no formato `<módulo>.<evento>`) sobre qual alvo, e quando. Só IDs, nunca dado pessoal; o banco recusa alteração e remoção |
| Conversa | `conversation` | Uma sessão de atendimento; é a *thread* do LangGraph |
| Mensagem | `message` | Sempre guardada já mascarada |
| Intenção | `intent` | A classe da mensagem (11 classes; ver `modelo-dominio.md`) |
| Agente | `agent` | **Só** o agente de IA (nó `support_agent`) |
| Proposta de ação | `ActionProposal` | O que o agente sugere; ainda não executado |
| Ação proposta (domínio) | `ProposedAction` | A proposta na forma que o motor avalia: tipo, situação, valor e itens. O `ActionProposal` do LLM é convertido nela na fronteira; uma proposta mal formada vira decisão negada, não erro |
| Situação da reclamação | `ClaimReason` | Por que o cliente reclama: `item_missing`, `wrong_item`, `quality_issue`, `late_delivery`, `not_delivered`, `duplicate_charge` ou `customer_cancellation`. Cada situação aceita só certos tipos de ação |
| Ação | `action` | Reembolso (`refund`), cupom (`coupon`) ou cancelamento (`cancellation`) executado |
| Motor de política | `policy` (`evaluate`) | O código que decide o que a política permite |
| Decisão da política | `PolicyDecision` | Permitida (`allowed`), precisa de aprovação (`needs_approval`) ou negada (`denied`) |
| Teto | `ceiling` | O maior valor que a política admite para o caso (item × quantidade, metade do item, cupom de atraso, total ainda não reembolsado). Acima dele, a proposta é negada |
| Código de motivo | `DenialReason`, `ApprovalReason` | Identificador estável em inglês do porquê da decisão. A negada leva o primeiro motivo; a que precisa de aprovação leva todos. Só se acrescentam valores |
| Histórico do cliente | `CustomerHistory` (`PriorAction`) | As ações já executadas que o motor pesa: as do cliente nos últimos 30 dias e todas as do pedido avaliado, com quem aprovou (`automatic` ou `reviewer`) |
| Limites da política | `PolicyLimits` (`POLICY_LIMITS`) | A fonte única dos números da política, em `policy/limits.py`. O nome de cada campo é o marcador nos documentos (`{{auto_approval_limit}}`) |
| Limite de autoaprovação | `auto_approval_limit` | Valor acima do qual a ação exige um humano |
| Documento da política | `PolicyDocumentId` (`PolicyDocument`) | `POL-01` a `POL-12`, em `policy/docs/`, com marcadores no lugar dos números, preenchidos por `render_policy_document` |
| Nota de fraude | `fraud_score` | Probabilidade calibrada de a reclamação não ter incidente real |
| Pedido de aprovação | `approval_request` | O caso que espera a decisão humana |
| Revisor ou atendente que aprova | `reviewer` | A pessoa no console. **Nunca** `agent` |
| Transferência | `handoff` | Passar a conversa para o atendimento humano, com resumo |
| Atendimento humano | `human_support` | A fila de pessoas que recebe as transferências |
| Mascaramento | `masking` (`privacy`) | Trocar dado pessoal por marcador antes do LLM e do log |
| Normalização | `normalize_text` | Desfazer truques de Unicode (invisíveis, largura total, outros alfabetos) antes de reconhecer dado pessoal ou palavra-chave |
| Marcador | `[CPF_1]`, `[CPF]` | O que fica no lugar do valor. Com número na conversa (`mask_text`), sem número no log (`redact_text`) |
| Registro de marcadores | `MarkerLedger` | O que uma conversa já viu, para o mesmo valor manter o marcador. Só guarda impressões digitais |
| Impressão digital | `fingerprint` | HMAC do valor com chave secreta e ID da conversa. Permite comparar sem guardar o valor |
| Trecho de política | `policy_chunk` | Pedaço indexado para o RAG, com ID citável (`POL-03#prazo`) |
| Modo degradado | `degraded_mode` | Sem LLM disponível: só texto fixo e transferência, nunca ação |
| Papel do modelo | `ModelRole` | `agent`, `writer`, `customer_sim`, `judge` ou `datagen` (tema 3). Cada papel tem modelo principal, reserva opcional, timeout, prazo total, teto de saída e temperatura |
| Referência de modelo | `ModelRef` | `provedor:modelo` (`gemini`, `groq`, `ollama` ou `anthropic`), como se escreve nas variáveis `LLM_<PAPEL>_MODEL` |
| Reserva (de orçamento) | `reserve` | Contar uma requisição no banco **antes** da chamada, numa instrução só; sem linha de volta, a chamada é recusada. Nunca é devolvida |
| Reserva (de modelo) | `fallback` | O segundo modelo de um papel, usado quando o principal falha ou não tem orçamento. Não confundir com a reserva de orçamento |
| Governor | `UsageGovernor` | O callback, um por modelo, que reserva o orçamento antes da chamada e grava o uso depois |
| Pausa do provedor | `ProviderGate` | O tempo em que um provedor que respondeu `429` não recebe chamadas: pelo `Retry-After` ou `retryDelay`, 5 s se ilegível, no máximo 1 h |
| Custo equivalente | `equivalent_cost_usd_micros` | O que a chamada custaria no preço de lista, mesmo na cota gratuita; vazio quando o preço ou os tokens são desconhecidos |
| Micros de dólar | `usd_micros` | Um milionésimo de dólar, inteiro: a unidade dos custos de `llm_calls` |
| Resultado de uma chamada de LLM | `LlmReply`, `DegradedMode` | O que `call_or_degrade` devolve: a resposta ou o modo degradado com o motivo |
| Cenário de mensagem | `MessageScenario` | O que o código sorteia antes do LLM: intenção, fatos, persona e marcadores pedidos. O rótulo vem dele |
| Variação | `variations` | Um dos 10 textos que o LLM escreve para um cenário; todas ficam do mesmo lado da divisão |
| Marcador | `[[CPF]]`, `[[INJECAO]]` etc. | Lugar no texto do LLM que o código preenche com um valor do Faker ou um ataque do catálogo |
| Assinatura de fatos | `facts_signature` | Hash estável da intenção e dos fatos de um cenário; garante que o teste de outro gerador não repete cenários |
| Hash dourado | `GOLDEN_HASH` | Hash da saída de um mundo pequeno fixado num teste: mudou a saída, sobe `DATAGEN_VERSION` |
| Manifesto do dataset | `manifest.json` (`Manifest`) | Versão, semente, commit, prompts, modelos, contagens e SHA-256 de cada arquivo de uma geração |
| Cenário de ponta a ponta | `E2EScenarioRecord` | Estado inicial, objetivo do cliente e estado final esperado de um fluxo F1–F7, usado pelos evals |
| Saída estruturada | `call_structured` | Chamada de LLM que devolve um objeto Pydantic validado ou o modo degradado (`INVALID_OUTPUT`) |
| Trecho da política | `policy_chunks`, `chunk_id` | Uma seção de um documento de política, com o texto preenchido e um ID citável (`POL-03#prazo`) |
| Busca híbrida | `SearchMode.HYBRID` | Lista lexical (full-text, termos com OU) e semântica (embedding) fundidas por RRF |
| RRF | `rrf` | Reciprocal Rank Fusion: soma de `1 / (60 + posição)` em cada lista; combina posições sem calibrar notas |
| Busca não confiável | `SearchResult.reliable` | Maior similaridade abaixo do limiar (0,85): o agente diz que não encontrou a regra em vez de inventar |
