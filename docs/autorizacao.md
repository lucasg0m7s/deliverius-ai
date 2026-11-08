# Autorização: quem pode ver e fazer o quê

A falha de segurança mais comum em APIs é deixar um usuário ler ou mudar o objeto de outro trocando um ID na
requisição. É o primeiro item do [OWASP API Security Top 10 (2023)](https://owasp.org/API-Security/editions/2023/en/0x11-t10/),
*Broken Object Level Authorization* (BOLA, também chamado de IDOR). Análise estática quase nunca a encontra, e
código gerado por IA a esquece com frequência, porque o caminho feliz funciona igual com ou sem a checagem.

Este documento define a regra para cada recurso e como ela é garantida. `seguranca.md`, seção 2, resume; o
detalhe é este.

## 1. Atores e escopos

| Ator | Como se identifica | Escopo |
| --- | --- | --- |
| **Cliente** (`customer`) | JWT com `role=customer`, `sub` = `customer_id` e `sandbox_id` | Os próprios pedidos e conversas, dentro da própria sandbox |
| **Revisor** (`reviewer`) | JWT com `role=reviewer` e `sandbox_id` | Fila de aprovações e transferências da própria sandbox |
| **Agente de IA** | Não tem token: roda dentro do turno de um cliente | O cliente e o pedido do **contexto de execução**, nunca de argumento do LLM |
| **Tarefas de fundo** | Processo da aplicação | Retenção e limpeza; nunca expostas por rota |
| **Anônimo** | Sem token | Só as rotas públicas da seção 3 |

**Dois limites se somam em toda consulta:** o da sandbox (`sandbox_id`), que separa visitantes da demo, e o do
dono (`customer_id`), que separa clientes dentro da sandbox. Fora da demo, a sandbox é uma só, e o limite do dono
continua valendo.

## 2. Matriz de acesso

| Recurso | Cliente | Revisor | Agente (ferramentas) |
| --- | --- | --- | --- |
| Pedido, itens, linha do tempo da entrega | Ler os próprios | Ler os ligados a uma aprovação ou transferência da fila | Ler o pedido do contexto |
| Resumo do histórico do cliente | — | Ler, no detalhe de uma aprovação | Ler o do cliente do contexto |
| Conversa e mensagens | Criar, ler e escrever nas próprias | Ler as ligadas à fila (texto mascarado) | Estado do próprio turno |
| Ação executada (reembolso, cupom, cancelamento) | Ler as próprias | Ler | Só propor; quem executa é o código |
| Pedido de aprovação | Ver só o status ("em análise") | Listar, ler e decidir | — |
| Nota e fatores do antifraude | **Nunca** | Ler | — |
| Transferência para humano | Ver que foi transferida | Listar, ler e assumir | Criar, pelo nó `handoff_to_human` |
| Auditoria | — | Ler a das ações da fila | — |
| Métricas do painel | — | Ler | — |
| Políticas (`POL-xx`) | Ler (são públicas) | Ler | `search_policy` |

Qualquer combinação que não está na matriz é **proibida por padrão**.

## 3. Rotas públicas

Só estas rotas aceitam requisição sem token; qualquer rota nova nasce protegida:

- `GET /health` e `GET /ready`;
- `POST /sandbox`, com limite por IP;
- `GET /docs` e `GET /openapi.json`.

## 4. Como a regra é garantida (em camadas)

1. **Rota protegida por padrão.** Os routers da API recebem a dependência de autenticação no próprio `APIRouter`,
   e não rota por rota. As públicas ficam num router separado, com a lista da seção 3.
2. **Papel na rota** (*function level*, BFLA): uma dependência `require_role("reviewer")` nas rotas do console.
3. **Escopo no repositório** (*object level*, BOLA): todo método de repositório que lê ou escreve dado de cliente
   recebe um `AccessScope` obrigatório (`sandbox_id` e, para o cliente, `customer_id`), e a consulta filtra por ele
   no próprio `WHERE`. Não existe `get_order(order_id)` sem escopo. Buscar por ID e conferir o dono depois, em
   Python, é proibido: é exatamente o padrão que se esquece.
4. **Objeto de outro → `404`, nunca `403`.** O `403` confirma que o ID existe; o `404` não revela nada. O `403` fica
   para papel errado numa rota (cliente chamando rota de revisor).
5. **ID não é segredo.** O UUID v7 é ordenado pelo tempo, então é parcialmente previsível. Nenhuma proteção depende
   de o ID ser difícil de adivinhar.
6. **Propriedades, não só objetos** (BOPLA, OWASP API3):
   - cada rota tem um modelo de **resposta** próprio por papel: o cliente nunca recebe a nota do antifraude, os
     fatores nem o motivo interno da política;
   - os modelos de **entrada** têm `extra="forbid"` e só os campos que o ator pode definir. O cliente não manda
     `status`, `amount_cents` nem `customer_id` (*mass assignment*);
   - o `customer_id` vem do token, nunca do corpo nem da URL.
7. **Agente:** o `customer_id` e o `order_id` vêm do contexto de execução, montado pela API a partir do token
   (`arquitetura.md`). Antes de rodar ou retomar um turno, a API confere que a conversa (`thread_id`) pertence ao
   cliente do token.
8. **Streams e links:** o SSE de uma conversa só abre para o dono. O link do trace no Langfuse, na aba "Por dentro",
   só aparece para a sandbox dona da conversa. Os traces só têm texto mascarado e dados sintéticos.
9. **Leitura de dado pessoal pelo revisor fica registrada** (quem abriu qual conversa, quando), como pede a LGPD
   para acesso a dados de terceiros (`seguranca.md`, seção 3).

## 5. Token

- JWT assinado com HS256 e algoritmo fixo na validação (nunca o `alg` do cabeçalho); `exp` de 15 minutos; `iss` e
  `aud` conferidos; 30 s de tolerância de relógio.
- Token só no cabeçalho `Authorization`, nunca na URL, porque URLs vão para logs e para o histórico do navegador.
  Para o front próprio, o stream usa `fetch` com cabeçalho, e não o `EventSource`, que não aceita cabeçalho.
- O token nunca vai para log nem para trace.
- Sem cookie de sessão, não há CSRF. Se um dia houver cookie, entram `SameSite=Strict` e um token anti-CSRF.

## 6. Como se testa

- **Matriz de autorização como teste.** Um teste parametrizado chama **toda rota** com cada ator (anônimo, cliente
  dono, outro cliente da mesma sandbox, cliente de outra sandbox, revisor, revisor de outra sandbox) e confere o
  status esperado (`200`, `401`, `403` ou `404`).
- **Rota nova sem linha na matriz quebra o build.** O teste percorre `app.routes` e falha se encontrar uma rota que
  não está na tabela do teste. É o que impede esquecer.
- **Repositório sem escopo não compila.** O `AccessScope` é parâmetro obrigatório e tipado; o mypy acusa a chamada
  sem ele.
- **Modelos de resposta:** um teste confere que nenhum campo proibido ao cliente (nota, fatores, motivo interno)
  aparece no JSON das rotas do cliente.
- **Agente:** um teste com o LLM falso pedindo o pedido de outro cliente prova que a ferramenta devolve o pedido do
  contexto, e não o pedido pedido.

## 7. Fora do núcleo, de propósito

- **Row Level Security do Postgres** como terceira barreira. O escopo no repositório mais os testes da matriz
  cobrem o risco com menos peças; o RLS entra se houver mais de uma aplicação acessando o banco.
- Papéis mais finos (supervisor, auditor) e permissões por ação. Dois papéis bastam para o domínio atual.
