# Entrega — Deliverius AI

Tema 10 da etapa de arquitetura, decidido em 2026-10-31. Define:
- os ambientes;
- a imagem Docker;
- a hospedagem gratuita;
- a demo e o front-end;
- a sandbox por visitante;
- o pipeline de entrega;
- o que o README precisa mostrar.

## 1. Ambientes

| Ambiente | Aplicação | Banco | LLM | Traces |
| --- | --- | --- | --- | --- |
| **Local** | `uv run fastapi dev` | `docker compose` (pgvector, porta 5433) | Gemini e Groq com as chaves do Lucas; Ollama opcional | Projeto `development` do Langfuse, ou desligado |
| **CI** | Testes | Testcontainers | Falso; os evals usam *secrets*, só na `main` | Desligado |
| **Demo** | Container único | **Neon** (gratuito) | Gemini (padrão) e Groq (reserva) | Projeto `demo` do Langfuse |

**Não há homologação.** Num projeto de uma pessoa, a proteção vem do CI, dos evals antes do deploy e de um teste de
fumaça depois dele.

## 2. Demo: hospedagem

| Peça | Escolha | Plano B |
| --- | --- | --- |
| **Aplicação** | **Hugging Face Spaces** (Docker, CPU Basic: 2 vCPU, 16 GB, dorme depois de 48 h sem uso), ao lado dos modelos e datasets no mesmo perfil | **Google Cloud Run** (camada gratuita, escala a zero, até 2 GB por instância). O autor já usa Google Cloud no dia a dia |
| **Banco** | **Neon** gratuito: 0,5 GB, computação desliga depois de 5 min sem uso e volta em menos de um segundo, projeto não pausa | Supabase (pausa o projeto depois de uma semana sem uso; pior para demo) |
| **Traces** | Langfuse Cloud (Hobby) | — |

**Antes da F5, conferir se é possível criar um Space Docker gratuito.** A documentação do Hugging Face se contradiz
(CPU Basic gratuito, mas criação de Space Gradio ou Docker exigindo PRO), e há relatos de erro de cota
([docs](https://huggingface.co/docs/hub/spaces-overview),
[fórum](https://discuss.huggingface.co/t/cpu-basic-spaces-hitting-quota-limits-despite-unlimited-hosting-inconsistent-behavior-with-restart-and-factory-rebuild/175681)).
Se não for possível, vai o Cloud Run, sem mudar nada na imagem.

**A demo acorda em segundos.** Space dormindo e Neon desligado somam uma espera na primeira visita. O README avisa
("a primeira resposta pode levar alguns segundos"), em vez de esconder.

## 3. Imagem Docker

- **Base:** multiestágio com `uv`, seguindo o guia oficial: o primeiro estágio instala as dependências do lock, o
  final é `python:3.13-slim`.
- **Só o grupo principal de dependências:** sem torch, transformers nem MLflow (tema 1). **Meta: imagem abaixo de
  1 GB.**
- **Modelos embutidos no build:** classificador ONNX, antifraude e e5 ONNX são baixados **no build**, pelas revisões
  fixas da configuração.
  - O container sobe sem rede para o Hugging Face e sem espera.
  - A imagem fica ligada às versões exatas dos modelos.
- **Usuário sem root,** UID 1000, que é o que o Spaces espera.
- **`HEALTHCHECK`** no `/ready`.
- **Migrations:** `alembic upgrade head` roda na subida, antes da aplicação. Com uma instância só isso é seguro; com
  várias, viraria um passo separado do deploy (anotado).
- **Trivy** examina a imagem no CI e barra vulnerabilidades críticas.

## 4. A demo (front-end do núcleo)

### Gradio agora, front próprio depois

| Opção | Decisão |
| --- | --- |
| **Gradio** montado no mesmo processo do FastAPI (`gr.mount_gradio_app`) | **Núcleo.** É Python, sai em dias e combina com o Spaces |
| Front próprio (React ou Next) com design system, a partir das telas listadas em `roadmap.md` (change `front-proprio`) | **Extra, depois do núcleo.** É aqui que o protótipo navegável e o design system voltam |

**A interface Gradio é cliente da API.** Ela chama as rotas HTTP da própria aplicação, com os tokens da sandbox, e
não as funções Python por dentro. A demo passa pela mesma porta de entrada, autenticação e autorização que um app
real usaria, e qualquer tela futura (o front próprio) usa a mesma API.

### Abas

| Aba | Conteúdo |
| --- | --- |
| **Cliente** | Lista de pedidos da sandbox; chat com o Deli; atalhos para os cenários de `fluxos-do-agente.md` ("simular: item faltando", "simular: reembolso alto"…); cartão de ação (proposta → executada) |
| **Atendente** | Fila com as abas Aprovações e Transferências; detalhe com a ação exata, nota, fatores, política e histórico; aprovar ou recusar com motivo |
| **Painel** | As métricas de `observabilidade.md`, seção 4 |
| **Por dentro** | Para cada turno: caminho no grafo, intenção, veredito, nota, tokens, custo e latência, mais o **link público do trace** no Langfuse (substitui a tela de rastreamento do protótipo) |
| **Laboratório** | As tabelas e gráficos dos relatórios: comparação dos classificadores, antifraude, RAG e evals por provedor |

## 5. Sandbox por visitante

Por que existe: deixar quem visita a demo testar tudo, inclusive aprovar, sem afetar os dados de outra pessoa
(`seguranca.md`, seção 2).

1. **Na primeira visita,** a interface chama `POST /sandbox`, que tem limite por IP.
2. **A API cria um `sandbox_id`** e semeia um conjunto pequeno de dados sintéticos: 2 clientes, cerca de 10 pedidos,
   com os pedidos dos cenários prontos.
3. **A API devolve dois tokens curtos,** um de `customer` e um de `reviewer`.
4. **Toda consulta filtra por `sandbox_id`,** e o repositório exige esse filtro, então não dá para esquecer.
5. **Uma tarefa de fundo,** iniciada no `lifespan` da aplicação, apaga as sandboxes com mais de 24 h a cada hora:
   conversas, ações, checkpoints e traces locais.

**Teto global:** um número máximo de sandboxes ativas e o orçamento diário de LLM (tema 3) protegem a cota de um
pico de visitas. Estourou → aviso honesto ("a demo atingiu o limite de hoje") e os relatórios estáticos continuam
disponíveis.

## 6. Pipeline de entrega

| Gatilho | O que acontece |
| --- | --- |
| PR | `ci.yml` (tema 7) |
| Merge na `main` | `ci.yml`, mais o build da imagem e o Trivy, **sem deploy** |
| Tag `vX.Y.Z` | Build, Trivy e envio ao Space (`git push` para o repositório do Space com o token do Hugging Face) ou ao Cloud Run. Depois, um **teste de fumaça**: `/ready` e uma conversa roteirizada pela API, com o provedor real e uma mensagem |
| Antes de criar a tag | A suíte núcleo dos evals e a de segurança rodadas na versão, com o relatório no PR de release |

**Versão:** SemVer, com o CHANGELOG gerado dos commits (Conventional Commits).

## 7. O README é parte da entrega

Quem chega ao projeto lê o README antes do código. Ordem:

1. **Uma frase do que é**, e o link da demo com um GIF de 20 segundos: cliente reclama → reembolso automático → caso
   alto vai para aprovação → atendente aprova.
2. **Os números:**
   - pass^1 e pass^3;
   - violações de política (zero);
   - recall dos casos sensíveis;
   - custo por mil conversas;
   - p95;
   - F1 do classificador no teste humano;
   - PR-AUC do antifraude;
   - recall@3 da busca.
3. **Diagrama da arquitetura** e as três decisões centrais em uma linha cada: o LLM só lê e propõe; política em
   código; aprovação humana com idempotência.
4. **Links:** modelos e datasets no Hugging Face, notebook no Kaggle, artigo, relatórios e ADRs.
5. **Como rodar localmente,** em 5 comandos.
6. **Limites honestos:** dados sintéticos, cota gratuita, o que ficou de fora.

## 8. Extras de entrega (depois do núcleo, nesta ordem)

1. **Demo pública** (seções 2 a 5).
2. **LoRA** (tema 5).
3. **Servidor MCP:** as quatro ferramentas de leitura expostas por *streamable HTTP* no mesmo processo, com
   autenticação. Prova o protocolo sem nova infraestrutura.
4. **n8n:** um fluxo que recebe um *webhook* de transferência de prioridade alta e avisa por e-mail, mais um relatório
   diário com as métricas do painel.
5. **Front próprio** com design system (seção 4).
