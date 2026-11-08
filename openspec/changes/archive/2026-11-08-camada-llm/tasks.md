## 1. Preparação

- [x] 1.1 `uv add langchain-core langchain-google-genai langchain-openai langchain-anthropic`; `uv.lock` atualizado, `uv run python -c "import langchain_openai, langchain_google_genai, langchain_anthropic"` funciona e `uv run pip-audit` não aponta nada novo
- [x] 1.2 Criar `src/deliverius/llm/`; colocar `llm` nas camadas, criar o contrato "provider integrations live only in llm" e proibir `deliverius.llm` nos utilitários; `uv run lint-imports` passa e falha ao importar `langchain_openai` em `orders` (desfeito em seguida)
- [x] 1.3 Variáveis de `design.md`, seção "Configuração", no `Settings` e no `.env.example`, com `ModelRef` validado; testes de referência válida, provedor desconhecido e formato inválido em `tests/unit/test_config.py`

## 2. Banco

- [x] 2.1 Tabelas `llm_calls` e `llm_budget_usage` em `db/tables.py` e migration `0003` com `lock_timeout`, `CHECK` e índices; o teste de migrations faz ida e volta
- [x] 2.2 `EXPLAIN` no teste de integração: custo por conversa e filtro por `created_at` usam os índices novos
- [x] 2.3 Integração: `CHECK` recusa tokens negativos e `status` desconhecido

## 3. Papéis, preços e prompts

- [x] 3.1 `roles.py`: papéis com timeout, prazo total, teto de saída e temperatura; teste de que todo papel tem configuração completa
- [x] 3.2 `pricing.py`: preços com a data da consulta e cálculo em micros de dólar; testes de arredondamento, modelo sem preço (`None`, nunca zero), token ausente e raciocínio que não soma duas vezes
- [x] 3.3 `prompts.py`: `load_prompt` com cabeçalho, hash e erros; testes com arquivos em `tmp_path` para cabeçalho inválido, versão inexistente, nome com `..` recusado e cabeçalho fora do texto devolvido

## 4. Falso

- [x] 4.1 `fake.py`: `ScriptedChatModel` com texto, ferramentas, estruturado, erro, travamento, tokens e erro claro no fim do roteiro; um teste por cenário da spec, com `bind_tools` e `with_structured_output` preservando o roteiro

## 5. Orçamento e registro

- [x] 5.1 `ports.py`, `in_memory.py` e `repository.py`: reserva, soma de tokens e gravação da linha; uma suíte de contrato em `tests/support/` roda contra os dois
- [x] 5.2 Integração, corrida: 50 reservas com `asyncio.gather` com 10 restantes geram 10 aceitas, 40 recusadas e contador 10
- [x] 5.3 Integração, corrida: somas de tokens com `asyncio.gather` não perdem nenhuma; limite de tokens barra a reserva seguinte
- [x] 5.4 Fronteiras: limite zero, exatamente no limite e um a mais, virada do dia com `FixedClock`, aviso de 80% uma vez só por tipo de limite
- [x] 5.5 `governor.py`: callback com reserva, registro de sucesso e de erro e latência; testes com o falso para chamada barrada sem tocar no modelo, metadados do prompt na linha, resposta sem tokens, falha ao gravar sem derrubar a resposta e banco fora do ar recusando a chamada
- [x] 5.6 Teste de regressão de conteúdo: um texto conhecido, na mensagem e no erro do provedor, não aparece em `llm_calls` nem nos logs capturados
- [x] 5.7 Teste de cancelamento: chamada cancelada não deixa linha, a reserva fica contada, e o registro de execuções não passa de 1.024 entradas (fronteira e uma a mais)
- [x] 5.8 Teste de `invoke` síncrono com o handler assíncrono: comportamento confirmado (recusa) e descrito no nome do teste; se a medição contrariar o design, registrar e ajustar antes de seguir

## 6. Fábrica e resiliência

- [x] 6.1 `factory.py` e `LlmRuntime.build`: os três tipos de integração, com modelo, timeout, teto de saída, `cache=False`, novas tentativas do SDK desligadas e `thinking_level` no Gemini; teste por provedor e teste de mensagem de chave faltando, que cita a variável e não o valor
- [x] 6.2 Com `httpx.MockTransport` na integração compatível com a OpenAI: `500` faz um pedido por tentativa; `stream_usage` traz os tokens no streaming; tokens de raciocínio chegam à linha
- [x] 6.3 `RoleModels.runnable()`: `with_retry` e `with_fallbacks`; testes de erro passageiro, erro não repetível, reserva assumindo com `llm.fallback_used`, papel sem reserva, e toda tentativa registrada
- [x] 6.4 Conferir que os erros de Gemini e Anthropic são traduzidos para as classes repetíveis de `langchain_core`, simulando os erros de cada SDK; ajustar a lista se a versão instalada divergir
- [x] 6.5 `ProviderGate`: pausa pelo `Retry-After`, pelo `retryDelay` do Gemini, padrão de 5 s (ilegível, negativo, data HTTP), teto de 1 h, espera de até 2 s e recusa acima disso; testes com `FixedClock` e fronteiras de 2 s e 1 h
- [x] 6.6 Com `MockTransport`, ponta a ponta: `429` com `Retry-After: 60` leva à reserva, e nenhuma requisição sai para o principal até o relógio andar 60 s
- [x] 6.7 Limitador: um `InMemoryRateLimiter` por provedor, a mesma instância para dois papéis, nenhum para Ollama e Anthropic; teste de identidade dos objetos, sem `sleep`
- [x] 6.8 `call_or_degrade`: resposta, todos falhando, orçamento esgotado em todos (nenhum modelo chamado), prazo total com o falso travado e cancelamento se propagando; o tipo de retorno é coberto por um `match` exaustivo que o mypy confere

## 7. Cache

- [x] 7.1 `cache.py`: `BaseCache` com SQLite e tentativa na chave; testes de acerto, tentativas independentes, ferramentas diferentes, parâmetros diferentes, resposta com erro não guardada e arquivo novo criado sozinho; `.cache/` no `.gitignore`
- [x] 7.2 Acerto com orçamento esgotado: responde, não gera linha em `llm_calls` e não conta; a chamada real de um modelo com cache soma a requisição sem barrar
- [x] 7.3 Modelo montado sem cache chama o provedor duas vezes, mesmo com um cache global definido no teste (e limpo em seguida); a chave de API, com um valor único, não aparece no arquivo do cache
- [x] 7.4 Teste de arquitetura: `set_llm_cache` não aparece em `src/`

## 8. Provedores reais

- [x] 8.1 `tests/llm/`, marcados `llm`: chamada com ferramenta e com saída estruturada, em Gemini e Groq, conferindo `llm_calls` com tokens maiores que zero e custo equivalente; sem a chave, o teste falha com mensagem que cita a variável, em vez de pular
- [x] 8.2 Conferir no teste real a temperatura (o `langchain_google_genai` já avisa que o `gemini-3.5-flash-lite` usa amostragem fixa e ignora a temperatura) e o `thinking_level` do Gemini, o local do `retryDelay` se um `429` aparecer e a saída estruturada do `openai/gpt-oss-120b`; divergência vira ajuste em `roles.py` e registro em `decisoes-arquitetura.md`
- [x] 8.3 **Depende do Lucas:** chaves do Gemini e do Groq no `.env`, retenção zero no Groq e cota conferida; ajustar os padrões no `.env.example` com os números da conta e rodar `uv run pytest -m llm --no-cov`, conferindo "passed" e nenhum pulado

## 9. Documentação

- [x] 9.1 `docs/arquitetura.md`: modelo do Gemini confirmado, cache próprio, novas tentativas numa camada só, callback, pausa do provedor, arquivos novos e regras de import; `docs/robustez.md`: reserva, dia em UTC, pausa e padrões
- [x] 9.2 `docs/decisoes-arquitetura.md`, com as alternativas: callback em vez de invólucro; SDKs sem novas tentativas; pausa do provedor; dia em UTC; orçamento que só conta em modelo com cache; cache próprio; modo degradado como resultado; dólar em micros; referência compacta de modelo; incluir a regra "sem chamada síncrona de modelo em `src/`" (tarefa 5.8: o `invoke` engole a recusa do governor)
- [x] 9.3 `docs/modelo-dominio.md` (`llm_budget_usage`, colunas de `llm_calls`), `docs/seguranca.md` (provedores conferidos), `docs/observabilidade.md` (eventos novos) e `docs/glossario.md` (papel, reserva, pausa do provedor, custo equivalente)
- [x] 9.4 `docs/roadmap.md`: escopo e critérios acrescentados na change 5; na change 12, os dois modelos no `ModelFallbackMiddleware` e o `call_or_degrade`; na change 13, a chave estrangeira com `NOT VALID` e a retenção de `llm_budget_usage`
- [x] 9.5 README: uma linha sobre os provedores e o orçamento, e o custo equivalente só depois do teste real, com o número medido

## 10. Verificação

- [x] 10.1 `uv run ruff format --check .`, `uv run ruff check .`, `uv run mypy` e `uv run lint-imports` passam
- [x] 10.2 `uv run pytest` passa, com cobertura total mínima de 85%, sem afrouxar nada
- [x] 10.3 `uv run pytest -m integration --no-cov` passa, com o Docker no ar, conferindo o número de "passed" e nenhum pulado
- [x] 10.4 `uv lock --check`, `openspec validate camada-llm --strict` e o `git grep` do `CLAUDE.local.md` vazio; nenhuma chave no repositório
