## 1. Preparação

- [x] 1.1 Grupo `datagen` no `pyproject.toml` com `faker` (sai do `dev`) e `huggingface-hub`, em `[tool.uv] default-groups` com o `dev`; `uv.lock` atualizado, `uv run pip-audit` sem nada novo e o `Dockerfile` conferido para não instalar o grupo
- [x] 1.2 Criar `src/deliverius/datagen/` e `src/deliverius/cli/`; import-linter: os dois como entradas (camada 5), contrato "nenhum módulo das camadas 0 a 4 importa `datagen` nem `cli`", e os dois nas fontes dos contratos de provedor e de `langchain_core`; `uv run lint-imports` passa e falha ao importar `langchain_core` em `datagen` (desfeito em seguida)
- [x] 1.3 `.gitignore` com `.cache/` e `data/generated/`; `LLM_DATAGEN_HOLDOUT_MODEL` e `HF_TOKEN` (`SecretStr`, opcional) no `Settings` e no `.env.example`, com testes em `tests/unit/test_config.py`

## 2. Camada de LLM

- [x] 2.1 Papel `datagen_holdout` em `roles.py` e na resolução de modelos; teto de saída de `datagen` e `datagen_holdout` em 2.048; testes: todo papel tem configuração, padrão do `datagen_holdout` em provedor diferente do `datagen`, chave do Groq faltando só falha quando o papel é pedido
- [x] 2.2 `RoleModels.structured_runnable(schema)` e `call_structured(...) -> T | DegradedMode` com `DegradedReason.INVALID_OUTPUT`; testes com o falso: saída válida grava linha de uso, campo ausente vira modo degradado sem o conteúdo no log, reserva responde depois das tentativas do principal, acerto de cache não conta no orçamento
- [x] 2.3 Atualizar o teste exaustivo do `match` sobre `DegradedReason` (mypy) e o teste de chamadas síncronas proibidas, que passa a cobrir `datagen` e `cli`

## 3. Base do gerador

- [x] 3.1 `config.py` (`DatagenConfig` com tamanhos, taxas, perfis, instante de referência, `DATAGEN_VERSION`) e `rng.py` (um gerador por camada derivado por SHA-256, UUIDv7 determinístico); testes: mesma semente e camada dão a mesma sequência, camadas diferentes divergem, UUID com versão 7 e ordenado pelo instante
- [x] 3.2 Teste de arquitetura: nenhum módulo de `datagen` chama `random.` global, `datetime.now`, `time.time` nem `uuid_utils.uuid7()`
- [x] 3.3 `records.py`: modelos Pydantic de cada linha gravada (cliente, pedido, reclamação, mensagem com posições de dado pessoal, par da política, cenário de ponta a ponta, manifesto); `output.py`: JSONL, escrita atômica em pasta temporária com manifesto por último, recusa de pasta existente sem `--overwrite`, leitura que confere o hash; testes de hash alterado, queda simulada antes do manifesto e pasta existente

## 4. Mundo, reclamações e resolução (sem LLM)

- [x] 4.1 `world.py`: clientes com perfil e dados do Faker pt-BR, restaurantes, seis meses de pedidos e linhas do tempo, incidentes nas taxas configuradas; testes: todo pedido constrói o modelo do domínio, toda linha do tempo segue `ALLOWED_TRANSITIONS`, CPF com dígito válido, proporção de perfis dentro de 2 pontos em 2.000 clientes
- [x] 4.2 `claims.py`: reclamações por perfil, casos difíceis e ruído de 2% a 3%; testes: fraudador sem incidente → `is_fraud`, honesto com incidente real → não fraude, fração de ruído na faixa e no manifesto, perfil e marca de ruído fora do arquivo de treino
- [x] 4.3 `resolution.py`: monta a `ProposedAction` do incidente e chama `policy.evaluate` com o histórico até o instante; testes dos dois cenários da spec (R$ 8,00 permitido com `POL-03`; 30 h depois negado por prazo) e um teste que confere que o módulo não importa nada de `policy` além da API pública
- [x] 4.4 `splits.py`, parte do antifraude: corte no último mês; teste contra vazamento (nenhuma reclamação do treino no corte ou depois) com propriedade do Hypothesis sobre instantes
- [x] 4.5 Teste do hash dourado: mundo pequeno com semente fixa contra hash fixado no teste, com mensagem que manda subir `DATAGEN_VERSION`; teste de sementes 42 e 43 diferentes

## 5. Mensagens, teste de outro gerador e pares da política

- [x] 5.1 `scenarios.py` e `placeholders.py`: sorteio de intenção, fatos, persona e marcadores; preenchimento com Faker e catálogo de ≈ 30 ataques em pt-BR; posições dos dados pessoais; testes: posições batem com o texto, marcador desconhecido e obrigatório ausente detectados, nenhum valor do Faker no texto enviado ao modelo
- [x] 5.2 Prompts `messages_v1.md`, `holdout_v1.md` e `rag_pairs_v1.md` em português, com cabeçalho de versão; fatos do cenário em bloco delimitado
- [x] 5.3 `messages.py`: 10 variações por chamada com `call_structured`, semáforo de 4, validação (marcadores, tamanho de 600 caracteres, repetição após `normalize_text`), até 3 tentativas por cenário com a tentativa no cache, montagem na ordem dos cenários; testes com o falso: intenção vem do cenário mesmo com texto de outro assunto, descarte contado e logado sem texto, ordem estável com respostas fora de ordem
- [x] 5.4 Parada por orçamento: `DegradedMode` de orçamento ou pausa interrompe, informa quantos faltam, sai com erro e não grava manifesto; teste com o falso e com o orçamento em memória; teste de retomada: segunda execução com o mesmo cache não faz chamada real nos cenários já feitos e gera arquivos idênticos
- [x] 5.5 Teste de outro gerador: fluxo aleatório separado, papel `datagen_holdout`, prompt próprio; teste de cenários disjuntos (identificador e assinatura de fatos)
- [x] 5.6 `splits.py`, parte das mensagens: 70/15/15 por cenário, estratificado; testes: nenhum cenário em dois lados, as 11 intenções em cada lado
- [x] 5.7 `rag_pairs.py`: perguntas por seção a partir de `render_policy_document`, filtro de 5 palavras seguidas; testes do filtro e do rótulo de documento e seção
- [x] 5.8 `attempts.py`: registro de tentativas no arquivo do cache (hash do papel, modelo, cenário e mensagens + número da tentativa que deu certo + contagens de descarte das tentativas anteriores, somadas de volta na execução seguinte); a execução seguinte começa nela; testes: tentativa 1 falha e a 2 acerta, segunda execução sem chamada real e arquivos idênticos byte a byte, inclusive o manifesto quando a primeira tentativa só gerou descartes; prompt alterado invalida o registro
- [x] 5.9 Pausa curta do provedor: `ProviderGate.seconds_left` e `LlmRuntime.pause_seconds_left`; em `PROVIDER_PAUSED` o gerador espera o tempo restante (`sleep` injetável) e repete a mesma chamada sem gastar tentativa; para se a pausa restante passar de 60 s ou houver mais de 10 pausas seguidas sem resposta válida; testes com relógio e `sleep` falsos

## 6. Cenários de ponta a ponta

- [x] 6.1 `e2e.py`: estado inicial, objetivo por modelo de frase, estado esperado por `evaluate` e pelas regras de F4, F5 e F6, decisão da política e rótulo de fraude gravados; testes: F2 com R$ 186,00 espera aprovação e nenhum reembolso, F5 espera transferência de prioridade alta sem ação, cada fluxo F1–F7 com ao menos 5 cenários, estado esperado de toda ação igual ao de `evaluate`

## 7. CLI, teste humano e publicação

- [x] 7.1 `cli/__main__.py` com os subcomandos `world`, `messages`, `holdout`, `rag-pairs`, `scenarios`, `all`, `validate-human` e `publish`; estimativa de chamadas impressa antes de começar; testes dos argumentos, código de saída e da estimativa
- [x] 7.2 `human_test.py` e `data/human-test/` (modelo CSV e README do formato); testes: intenção desconhecida, texto vazio ou longo, id repetido, CPF detectado sem o texto impresso
- [x] 7.3 `publish.py`: card gerado do manifesto (sintético, como foi gerado, tamanhos, divisões, limites, licença), configs no YAML do card, sem confirmação só lista, sem `HF_TOKEN` falha citando a variável, tag já existente recusada; testes com um cliente falso do Hugging Face e com um token de valor único que não aparece em log nem em erro
- [x] 7.4 Eventos de log de `design.md` e teste de que nenhum texto de mensagem, prompt ou dado do Faker aparece nos logs capturados de uma geração com o falso

## 8. Integração e geração real

- [x] 8.1 Integração (Postgres): geração de mensagens com o falso pela `LlmRuntime` real, gravando `llm_calls` e respeitando o orçamento; orçamento esgotado no meio para a execução sem manifesto
- [x] 8.2 `-m llm`: um cenário real no Gemini (`datagen`) e um no Groq (`datagen_holdout`) com saída estruturada válida e linha de uso com tokens e custo
- [ ] 8.3 Geração completa com semente fixa (`all`), dividida em dias se a cota pedir; conferir contagens contra `modelo-dominio.md`, seção 4, e descarte ≤ 20% por intenção
- [x] 8.4 Revisão à mão de 100 mensagens estratificadas; defeitos contados em `docs/dados-sinteticos.md`, com descartes, diversidade, taxa de fraude e ruído
- [ ] 8.5 Amostras de até 50 linhas por conjunto em `data/samples/` e `data/README.md`; conferir que as amostras não têm nada além de dados sintéticos

## 9. Documentação

- [x] 9.1 `modelo-dominio.md` seção 4 (divisão 70/15/15, formato, marcadores, papel `datagen_holdout`), `arquitetura.md` (papel novo, `call_structured`, contratos), `ml-e-avaliacao.md` (linhagem pelo manifesto), `seguranca.md` (nada pessoal ao LLM no gerador, `HF_TOKEN`), `glossario.md` (cenário, variação, marcador, hash dourado), `roadmap.md` (escopo detalhado) e `decisoes-arquitetura.md` (as decisões de `design.md` com alternativas)
- [ ] 9.2 README: seção "Dados sintéticos" com os comandos na ordem, os tamanhos, o link do dataset no Hugging Face (depois da publicação) e o número da revisão à mão

## 10. Publicação (só com autorização)

- [ ] 10.1 Com o `HF_TOKEN` no `.env`, a licença confirmada e a autorização dada: `publish` sem confirmação para revisar o card, depois com confirmação; conferir a tag `v<versão>` e o carregamento por `datasets.load_dataset` de cada config

## 11. Verificação

- [x] 11.1 `uv run ruff format --check .` e `uv run ruff check .`
- [x] 11.2 `uv run mypy`
- [x] 11.3 `uv run lint-imports`
- [x] 11.4 `uv run pytest` (cobertura mínima de 85%)
- [x] 11.5 `uv run pytest -m integration --no-cov`, conferindo o número de testes passados (sem o Docker eles pulam com sucesso)
- [ ] 11.6 `uv run pytest -m llm --no-cov`
- [x] 11.7 `openspec validate gerador-dados --strict` e o `git grep` do `CLAUDE.local.md` vazio
