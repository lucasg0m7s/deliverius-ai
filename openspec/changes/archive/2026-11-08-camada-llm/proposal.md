## Why

Change 5 do `docs/roadmap.md` (`camada-llm`, fase F1), que cobre os requisitos **R03** (modelos em operação), **R13**
(vários provedores) e **R21** (observabilidade, custo e latência). O gerador de dados (change 6) e o agente (change 12)
precisam chamar LLMs, e a cota gratuita é o recurso mais escasso do projeto: sem papéis, reserva, orçamento e registro
de uso numa camada só, cada chamador refaria essas regras, e uma falha de provedor poderia virar erro para o cliente.

## What Changes

Escopo copiado do roadmap:

- `llm/` como em `arquitetura.md`, seção "Camada de LLM": papéis, fábrica (Gemini, compatível com OpenAI, Anthropic),
  preços e prompts versionados.
- Registro de uso na tabela `llm_calls`, com o custo real e o equivalente.
- Orçamento diário com **reserva atômica** antes da chamada e modo degradado.
- Limite de requisições por provedor, respeitando o `Retry-After` de um `429`.
- Cache só para evals e geração de dados, com o número da tentativa na chave.
- Modelo falso roteirizado para os testes.

Detalhado a partir do escopo (o roadmap é atualizado junto):

- **Uma camada só de novas tentativas:** as do SDK de cada provedor ficam desligadas, para toda tentativa passar pelo
  orçamento e pelo registro.
- **Pausa do provedor depois de um `429`:** enquanto dura a espera pedida, nenhuma chamada sai para aquele provedor; a
  chamada vai para a reserva ou para o modo degradado.
- **Tabela `llm_budget_usage`**, além da `llm_calls`, para o contador do orçamento por provedor e dia.
- **Chamada com modo degradado como resultado tipado:** o chamador trata "sem LLM" como um caso, sem capturar erro de
  provedor.
- **Contrato novo do import-linter:** as integrações de provedor só em `llm/`.

## Fora de escopo

Do roadmap:

- Prompts do agente (change 12).
- Spans do OpenTelemetry (change 14).

Decorrentes:

- `ModelRetryMiddleware`, `ModelFallbackMiddleware` e os limites de chamadas do `create_agent` (change 12): o pacote
  `langchain` entra lá.
- `bootstrap.py` (change 12): esta change entrega a montagem, e a falha na subida passa a valer quando o `bootstrap` a
  chamar.
- Mascaramento antes do LLM (`mask_input`) e caminho sensível sem LLM (change 12).
- Chave estrangeira de `llm_calls` para `conversations` e a tarefa de retenção de 90 dias (change 13).
- `trace_id` preenchido (change 14) e visões do painel (extra E1).
- Chamada real à Anthropic: só teste da fábrica, sem crédito de API.
- Nenhuma rota nova.

## Critério de pronto

Copiado do roadmap:

- A fábrica tem teste para os três provedores.
- Um teste `-m llm` faz uma chamada real ao Gemini e outra ao Groq e registra tokens e custo.
- O orçamento estourado leva ao modo degradado, num teste com o falso.
- Chamadas disparadas juntas nunca passam do orçamento (teste de integração com `asyncio.gather`).

Acrescentado por esta change:

- Um `429` com `Retry-After` pausa o provedor pelo tempo pedido, e a chamada seguinte vai para a reserva.
- A mesma tentativa repetida não gasta cota, e tentativas diferentes geram respostas independentes.
- Um teste prova que nenhum conteúdo de mensagem nem a chave de API chega à `llm_calls` ou ao log, e que a chave não
  chega ao cache.
- Sem a chave do provedor de um papel configurado, a montagem falha com mensagem que cita a variável.

## Capabilities

### New Capabilities

- `llm`: papéis e provedores, reserva e modo degradado, orçamento diário, limite de requisições, registro de uso e
  custo, prompts versionados e cache de evals.

### Modified Capabilities

Nenhuma.

## Impact

- **Código novo:** `src/deliverius/llm/`, `tests/unit/llm/`, `tests/integration/test_llm_*.py`, `tests/llm/`.
- **Banco:** migration `0003`, aditiva, com `llm_calls` e `llm_budget_usage`.
- **Dependências:** `langchain-core`, `langchain-google-genai`, `langchain-openai` e `langchain-anthropic`.
- **Configuração:** variáveis novas de papéis, chaves e limites no `.env.example`. Tudo opcional no `Settings`, porque a
  API ainda não usa LLM.
- **Qualidade:** `pyproject.toml` (contratos do import-linter).
- **Documentação:** `arquitetura.md`, `robustez.md`, `seguranca.md`, `modelo-dominio.md`, `observabilidade.md`,
  `decisoes-arquitetura.md`, `glossario.md`, `roadmap.md`, `README.md`.
- **Pendências humanas:** as chaves do Gemini e do Groq e a conferência da cota. O teste `-m llm` só pode ser concluído
  com elas.
- **API e grafo:** nenhuma mudança.
