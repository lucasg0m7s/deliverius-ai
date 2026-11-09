## 1. Preparação

- [x] 1.1 `pyproject.toml`: `lightgbm` nas dependências (depois da escolha, movido para o grupo `research`: ver 5.0) e o grupo `research` (`pandas`, `pandas-stubs`, `scikit-learn`, `shap`, `mlflow`, `matplotlib`, `pyyaml`) fora de `default-groups`; conferir na documentação da versão instalada `pred_contrib`, `raw_score`, `FrozenEstimator`, `TunedThresholdClassifierCV`, o *metadata routing* e se o `lightgbm` traz `py.typed`; `uv sync --group research` passa
- [x] 1.2 `mypy` cobrindo `research/` (com `ignore_missing_imports` para scikit-learn, shap, mlflow, e `lightgbm` se não tiver tipos); `pytest` com `research` importável; `uv run mypy` passa
- [x] 1.3 Criar `src/deliverius/fraud/` e `src/deliverius/artifacts/`; import-linter: `fraud` na camada de capacidades sem importar `actions`/`approvals`, `artifacts` entre os utilitários, contrato novo "`lightgbm` só em `fraud`" (depois da escolha: "bibliotecas de pesquisa nunca na aplicação"), e os dois nas listas dos contratos de provedor de LLM, `langchain_core` e `onnxruntime`; `uv run lint-imports` passa
- [x] 1.4 `config.py`: `fraud_model_dir` (padrão `models/fraud`) e `FRAUD_MODEL_DIR` no `.env.example`; teste do padrão em `test_config.py`
- [x] 1.5 `Dockerfile`: `libgomp1` no estágio final; `docker build` passa e `python -c "import lightgbm"` roda dentro da imagem (feito e conferido; revertido depois da escolha, porque a imagem não leva mais o LightGBM)

## 2. Artefatos com revisão fixa

- [x] 2.1 `artifacts/pinned.py`: `ModelFile`, `PinnedRepository(repo_id, revision, repo_type)`, `verify_files`, `fetch_files`, `ArtifactFileError`, `ArtifactDownloadError` (subclasse de `ExternalServiceError`), eventos `model_files.verified|rejected|download_failed` com `repository`; testes unitários movidos e generalizados (faltando, tamanho, hash, parcial apagado, já correto não baixa, falha do hub)
- [x] 2.2 `retrieval/model_files.py` passa a delegar para `artifacts`, mantendo as funções e erros públicos; os testes da busca e o `retrieval fetch-model` passam sem mudança de comportamento

## 3. Features no ponto do tempo

- [x] 3.1 `fraud/models.py`: `CustomerFacts`, `PriorClaim` (instante, tipo, valor pedido, se foi à revisão), `CustomerClaimHistory`, `ClaimContext`, `FraudFactor`, `FraudScore` com invariantes (instantes com fuso, probabilidade em [0, 1]); testes das invariantes
- [x] 3.2 `fraud/features.py`: `FEATURE_SCHEMA`, `FEATURES_VERSION`, `schema_hash()` e `compute_features(context)` com o filtro estritamente anterior, janelas de 30 e 90 dias, teto de 1.000, `NaN` sem entrega, indicadores dos enums e hora em UTC−3; testes dos cenários da spec (cliente sem histórico, mesmo instante, entrega depois da reclamação, divisão por zero, teto)
- [x] 3.3 Propriedade (Hypothesis): históricos com fatos posteriores e no mesmo instante acrescentados dão o mesmo vetor; esse é o teste "nenhuma feature usa dado posterior à reclamação" do critério de pronto
- [x] 3.4 Testes do esquema: hash dourado (mudou o esquema sem subir a versão → falha), lista exata de nomes, nenhum nome com campo proibido (`address`, `zip`, `cep`, `district`, `city`, `name`, `cpf`, `phone`, `email`, `message`, `profile`, `label`, `reviewer`)
- [x] 3.5 `fraud/context.py`: `claim_context(order, proposal, claimed_at, history)` puro; testes com pedido entregue, não entregue e cancelado
- [x] 3.6 `fraud/decision.py`: `needs_review` (≥, com centavos inteiros e probabilidade) e `top_factors` (3 maiores em valor absoluto, com sinal, sem o termo de base, desempate pela ordem do esquema); testes dos cenários de R$ 8, R$ 180 e "igual ao custo"
- [x] 3.7 CI: passo "fraud features need 100% branch coverage" (`pytest tests/unit/fraud --cov-reset --cov=deliverius.fraud.features --cov-fail-under=100`); passa localmente

## 4. Pesquisa (`research/fraud/`)

- [x] 4.1 `config.yaml` (semente, meses, grade, custo de 400 centavos, *bootstrap* de 2.000, pisos) e `dataset.py`: lê `data/generated/v1.0.0` por `verify_dataset`/`read_records`, tira as reclamações negadas, monta o histórico de cada reclamação a partir de `fraud_history` e `world` e chama `claim_context` + `compute_features`; nunca lê `fraud_ground_truth`; testes: dataset com hash errado é recusado antes de qualquer feature, e a reclamação no mesmo instante fica fora do histórico
- [x] 4.2 `split.py`: treino de maio a agosto, validação em setembro, teste em outubro e as três dobras de janela crescente; testes: nenhum instante do teste antes de 01/10, nenhuma dobra valida um mês anterior ao treino
- [x] 4.3 `evaluate.py`: PR-AUC, ROC-AUC, Brier, confiabilidade em 10 faixas por quantil, recall nos 5% mais suspeitos, custo esperado por regra e *bootstrap* pareado da diferença de PR-AUC; testes com vetores pequenos de resultado conhecido
- [x] 4.4 `train.py`: regra de negócio, regressão logística (com imputação e padronização), grade do LightGBM com as dobras, calibração sigmoide com `FrozenEstimator` em setembro, `TunedThresholdClassifierCV` com o *scorer* de custo, regra de escolha do design aplicada só na validação, e registro no MLflow (parâmetros, métricas, versão do dataset, commit, semente, gráficos); teste de determinismo: duas execuções num subconjunto dão as mesmas probabilidades
- [x] 4.5 Rodar o treino completo e a avaliação do teste uma vez; guardar a execução do MLflow; conferir que o calibrador aplicado à mão (`a`, `b`) bate com o `CalibratedClassifierCV` até 1e-9 em todo o teste
- [x] 4.6 `docs/antifraude.md`: as tabelas por modelo e por regra (com intervalos), a curva de confiabilidade, a curva PR, SHAP global, a escolha pela regra, os limites (sintético, poucos positivos, aquecimento de maio a julho) e os pisos do CI

## 5. Aplicação: pontuação e carga

**5.0 Resultado da regra de escolha (só setembro, antes do teste):** PR-AUC da logística 0,396 [0,307; 0,535],
do LightGBM 0,301 [0,234; 0,426]; diferença LightGBM − logística [−0,182; −0,007], toda abaixo de zero. Pela regra do
design, fica a **regressão logística**, que vence a regra de negócio ([+0,149; +0,382]). Consequências, já previstas no
design ("só o vencedor ganha adaptador"): o adaptador é o `LogisticFraudScorer` em numpy, sem `pred_contrib`, sem
thread nem trava (são 27 multiplicações); o `lightgbm` sai da aplicação para o grupo `research` e o `libgomp1` sai da
imagem; a exceção do `.predict(` (5.3) não é necessária, e a regra do teste de arquitetura continua valendo para todo o
`src/` sem lista de liberados.

- [x] 5.1 `fraud/ports.py` (`FraudScorer`, `FraudContextReader`) e `fraud/fake.py` (`FixedFraudScorer`); teste do fake
- [x] 5.2 `fraud/scorer.py` (feito o `LogisticFraudScorer`, ver 5.0): adaptador do modelo escolhido (`LightGbmFraudScorer` com `raw_score` e `pred_contrib`; ou `LogisticFraudScorer` se a logística vencer), calibrador sigmoide, `needs_review`, `top_factors`, `asyncio.to_thread` com trava e evento `fraud.scored`; testes com um modelo minúsculo treinado no próprio teste: soma das contribuições = saída bruta (1e-6), fatores ordenados, revisão pela regra, evento sem dado pessoal
- [x] 5.3 ~~Teste de arquitetura: `ALLOWED_SYNC_PREDICT` com `deliverius/fraud/scorer.py`~~ Não se aplica (5.0): o adaptador logístico não chama `.predict(`; conferido: o teste de arquitetura passa sem exceção nenhuma
- [x] 5.4 `fraud/model_files.py`: `REPOSITORY`, `REVISION`, `MANIFEST` fixados; `load_fraud_model(dir)` conferindo o manifesto, depois os arquivos listados, depois o `feature_schema_hash` (`fraud_model.schema_mismatch` com evento dos dois hashes); `fetch_fraud_model(dir)`; testes com um artefato montado no teste: arquivo trocado, faltando, esquema diferente, download repetido
- [x] 5.5 `cli/fraud.py`: `fraud fetch-model` (sai com 0, 1 ou 2 como o `retrieval`), textos em português; testes da CLI com o downloader falso

## 6. Leitor do contexto (Postgres)

- [x] 6.1 Migration `0006`: índice `ix_approval_requests_sandbox_id_customer_id_claimed_at` (nome pela convenção de `tables.py`) `(sandbox_id, customer_id, claimed_at)` com `CREATE INDEX CONCURRENTLY` em `autocommit_block`; `db/tables.py` atualizado; o teste de migrations faz ida e volta
- [x] 6.2 `fraud/repository.py`: `PostgresFraudContextReader.history(scope, claimed_at)` com as três consultas do design, `LIMIT 1000`, escopo no `WHERE` e `translate_db_errors`; versão em memória para os unitários
- [x] 6.3 Integração: aprovação que virou ação conta uma vez; aprovação recusada conta como reclamação e como revisão; ação no mesmo instante e posterior fora; outro cliente e outra sandbox fora; teto de 1.000; `EXPLAIN` das três consultas usa os índices (sem `Seq Scan` nas tabelas grandes)
- [x] 6.4 Teste de paridade: um histórico gravado no Postgres e o mesmo histórico montado como no `research/fraud/dataset.py` dão o mesmo vetor de features

## 7. Publicação e porta do CI

- [x] 7.1 `research/fraud/publish.py`: monta `model.txt` (ou `logistic.json`), `calibrator.json`, `decision.json`, `manifest.json` e o card em inglês numa pasta temporária; ensaio por padrão mostra arquivos, hashes e destino; `--confirm` cria o repositório `G0m7s/deliverius-fraud`, envia e cria a tag; recusa tag existente; testes com o hub falso (ensaio não envia, tag existente recusa, queda entre envio e tag é retomada)
- [ ] 7.2 **Com a autorização do autor:** publicar `v1.0.0`; fixar `REVISION` e o `MANIFEST` em `fraud/model_files.py`; `fraud fetch-model` baixa e `load_fraud_model` aceita
- [x] 7.3 `research/fraud/gate.py`: baixa o dataset (commit da tag `v1.0.0`, manifesto fixado) e o modelo, pontua outubro com o *scorer* da aplicação e falha abaixo do piso de PR-AUC ou acima do teto de Brier, mostrando valor e limite; teste com um modelo ruim de propósito → falha
- [x] 7.4 `ci.yml`: `uv sync --locked --group research`, cache de `models/fraud` e de `data/generated/v1.0.0` com as chaves das constantes, `fraud fetch-model`, `python -m research.fraud.gate`; teste que mantém as chaves de cache iguais às constantes (como `test_ci_model_cache.py`)

## 8. Documentação

- [x] 8.1 `docs/ml-e-avaliacao.md` 1.4: as features como ficaram ("enviadas à revisão em 90 dias", `requested_fraction`, teto, UTC−3), a população pontuada, a divisão com as dobras por mês e a regra de escolha
- [x] 8.2 `docs/decisoes-arquitetura.md`: definição de reclamação igual nos dois lados, `artifacts` genérico e a troca dos eventos `retrieval.model_*`, pino no código, sem *pickle*, calibrador sem scikit-learn na aplicação, exceção do `.predict(`, grupo `research` fora da imagem, regra de escolha na validação
- [x] 8.3 `docs/arquitetura.md` (pastas `fraud/` e `artifacts/`, `research/fraud/`) e `docs/roadmap.md` (carga de artefatos nasce na 10 e é reaproveitada pela 9; a falha ao pontuar manda para humano na 12)
- [x] 8.4 README: PR-AUC, Brier e custo no teste contra a regra de negócio, link do modelo no Hugging Face, comandos da pesquisa e change 10 no status
- [x] 8.5 Conferir o checklist de `docs/seguranca.md`, seção 7 (artefato conferido por hash, sem *pickle*, nenhum dado pessoal em feature ou log)

## 9. Verificação

- [x] 9.1 `uv run ruff format --check .`
- [x] 9.2 `uv run ruff check .`
- [x] 9.3 `uv run mypy`
- [x] 9.4 `uv run lint-imports`
- [x] 9.5 `uv run pytest` (cobertura mínima de 85% e 100% dos ramos em `policy`, `privacy`, `actions` e `fraud.features`)
- [x] 9.6 `uv run pytest -m integration --no-cov`
- [ ] 9.7 `uv run python -m research.fraud.gate` passa com o modelo publicado
- [x] 9.8 `openspec validate antifraude-reembolso --strict`
