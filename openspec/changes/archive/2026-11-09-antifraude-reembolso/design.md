## Context

O que já existe e é usado:

- **Dataset `v1.0.0`** (`G0m7s/deliverius-ai`, change 6), com o SHA-256 de cada arquivo no `manifest.json`, lido e
  conferido por `datagen.output.verify_dataset` e `read_records`:
  - `fraud_history/train.jsonl` (2.880 reclamações, de maio a setembro) e `test.jsonl` (498, de outubro;
    `fraud_test_cutoff` = 2026-10-01);
  - `world/` (2.000 clientes, 20.000 pedidos de 01/05 a 26/10, com eventos de entrega);
  - `fraud_ground_truth/` (perfil, incidente, rótulo trocado), que **nunca** vira feature.
- **Distribuição medida:** taxa de fraude de 11,8%, com 55 a 74 fraudes por mês (setembro 73, outubro 59). São 219
  reclamações negadas pela política, 681 que precisam de aprovação e 2.478 permitidas.
- **Como a aplicação grava uma reclamação** (change 11):
  - `approval_requests` guarda a proposta, o `claimed_at` e a decisão;
  - `actions` guarda a ação executada, com `approval_request_id` quando veio de uma aprovação;
  - a reclamação negada pela política **não é gravada**.
- `approvals.FraudAssessment(score, model_version, factors)`, já pronto para receber a nota (change 11).
- `retrieval/model_files.py`, com revisão fixa, tamanho e SHA-256 por arquivo, `fetch` que apaga arquivo parcial e
  o `ModelDownloadError` como `ExternalServiceError`.
- O teste `test_architecture_sync_model_calls.py`, que proíbe `.predict(` em todo `src/`.
- No grafo (`arquitetura.md`), o `score_fraud` só roda depois de a política **não negar** a proposta.

## Goals / Non-Goals

**Goals:**

- `fraud/features.py` puro, com 100% dos ramos cobertos e a prova de ponto no tempo por teste de propriedade.
- A pesquisa reproduzível em `research/fraud/`, com a regra de escolha escrita aqui, antes de rodar.
- Os adaptadores `FraudScorer` e `FraudContextReader`, a carga conferida do modelo e a porta de qualidade no CI.
- O modelo publicado e fixado numa revisão.

**Non-Goals:**

- O nó `score_fraud`, o mapeamento `FraudScore → FraudAssessment` e a carga do modelo na subida da API (change 12).
- O `GET /ready` (change 9).
- O notebook e o dataset no Kaggle (trilha de publicação).
- Deriva, retreino e calibração isotônica.

## Decisions

### Estrutura

```
src/deliverius/
├── artifacts/               camada de utilitários (como db): arquivos de modelo numa revisão fixa
│   └── pinned.py            ModelFile, PinnedRepository(repo_id, revision, repo_type), verify_files,
│                            fetch_files, ArtifactFileError, ArtifactDownloadError (vindos de retrieval/model_files)
├── retrieval/model_files.py só as constantes do e5 e as funções de antes, delegando para artifacts
├── fraud/
│   ├── models.py            CustomerFacts, PriorClaim, CustomerClaimHistory, ClaimContext, FraudScore, FraudFactor
│   ├── features.py          FEATURE_SCHEMA (nomes, ordem, tipos), FEATURES_VERSION, schema_hash(), compute_features
│   ├── context.py           claim_context(order, proposal, claimed_at, history): monta o ClaimContext (puro)
│   ├── decision.py          needs_review(probability, amount, review_cost) e top_factors(contributions)
│   ├── ports.py             FraudScorer (async score), FraudContextReader (async history)
│   ├── scorer.py            LightGbmFraudScorer: Booster + calibrador sigmoide + regra; pred_contrib
│   ├── model_files.py       repositório, revisão e manifesto fixados; load_fraud_model(dir) → FraudModel
│   ├── repository.py        PostgresFraudContextReader
│   └── fake.py              FixedFraudScorer, para os testes e para o grafo da change 12
└── cli/fraud.py             `fraud fetch-model`
research/
└── fraud/
    ├── config.yaml          semente, meses, grade de hiperparâmetros, custo da revisão, pisos do CI
    ├── dataset.py           lê o dataset conferido e monta um ClaimContext por reclamação (mesmo claim_context)
    ├── split.py             divisão por mês
    ├── train.py             baselines, LightGBM, calibração, limiar; registra no MLflow
    ├── evaluate.py          métricas, bootstrap, curva de confiabilidade, custo, SHAP (gráficos do relatório)
    ├── publish.py           monta e publica o artefato (ensaio por padrão; `--confirm` envia)
    └── gate.py              porta do CI
```

`research/` é um pacote na raiz, fora da imagem, rodado com `python -m research.fraud.<etapa>` (princípio de
`ml-e-avaliacao.md` 1.2: scripts com YAML, e o notebook só chama os scripts). Ele importa `deliverius.fraud` e
`deliverius.datagen`; a aplicação nunca importa `research`.

### Uma reclamação, igual no treino e na aplicação

O plano previa "reclamações recusadas antes". A aplicação não grava a negativa da política, então essa feature não
poderia ser calculada na hora de pontuar: o treino veria um valor que a aplicação nunca vê (*training-serving
skew*). A definição passa a ser a seguinte, a mesma dos dois lados:

| Conceito | Treino (`fraud_history`) | Aplicação (Postgres) |
| --- | --- | --- |
| Reclamação anterior | linha do mesmo cliente com `claimed_at` anterior e `decision ≠ denied` | `approval_requests` do cliente, mais `actions` com `approval_request_id IS NULL` |
| Instante | `claimed_at` | `approval_requests.claimed_at`; `actions.executed_at` (a ação automática é executada na hora) |
| Enviada à revisão | `decision = needs_approval` | toda linha de `approval_requests`, em qualquer status |
| Valor pedido | `requested_amount_cents` | o valor da proposta, ou o `amount_cents` da ação |

- **População pontuada:** só reclamações não negadas, porque o `score_fraud` só roda nelas. São cerca de 3.159 das
  3.378. A negada sai do treino e do teste.
- **O desfecho do revisor nunca entra:** a recusa quase sempre significa fraude (decisão de 2026-11-08).

### Features

Todas são números (`float`); a ausente vira `NaN`. As categorias viram indicadores 0/1 de enums fechados, para que a
regressão logística e o LightGBM recebam o mesmo vetor e não haja um mapa de categorias que possa divergir entre
treino e aplicação.

| Grupo | Features |
| --- | --- |
| Conta | `account_age_days` |
| Histórico (90 d) | `orders_90d`, `claims_30d`, `claims_90d`, `refund_requested_30d_cents`, `claims_per_order_90d`, `reviews_90d` |
| Pedido e reclamação | `order_total_cents`, `requested_cents`, `requested_fraction`, `minutes_since_delivery` (NaN sem entrega), `marked_delivered`, `local_hour` |
| Indicadores | `action_<kind>`, `reason_<reason>`, `payment_<method>` |

- **`requested_fraction`** (pedido ÷ valor do pedido) entra além do plano: o oportunista do gerador exagera o valor,
  e a fração é o sinal de exagero que não depende do tamanho do pedido.
- **Ponto no tempo:** `compute_features` filtra ele mesmo tudo o que não é **estritamente anterior** a `claimed_at`
  (pedidos, eventos de entrega, reclamações). Não confia em quem chama. Um teste com Hypothesis gera históricos com
  fatos posteriores e no mesmo instante e confere que o vetor não muda.
- **Hora local:** fuso fixo UTC−3, porque o Brasil não tem horário de verão desde 2019. Evita depender do `tzdata`
  no Windows.
- **Teto de contagem:** 1.000 por janela, igual nos dois lados. O leitor busca no máximo 1.000 linhas por consulta,
  e a contagem satura no teto.
- **Esquema:** `FEATURES_VERSION` mais nomes, ordem e tipos, num JSON canônico com SHA-256 (`schema_hash`). Um teste
  de hash dourado obriga a subir a versão quando o esquema muda, como no `DATAGEN_VERSION`.
- **Campos proibidos:** um teste confere que nenhum nome do esquema contém `address`, `zip`, `cep`, `district`,
  `city`, `name`, `cpf`, `phone`, `email`, `message`, `profile`, `label` nem `reviewer`.

### Divisão e aquecimento

| Meses | Uso |
| --- | --- |
| maio a agosto | treino e escolha de hiperparâmetros (janela crescente: treina até o mês k e valida no k+1, três dobras) |
| setembro | validação: calibração, limiar do `TunedThresholdClassifierCV` e escolha do modelo |
| outubro | teste, uma vez só |

- **Os pedidos começam em 01/05,** então as janelas de 90 dias ficam incompletas até o fim de julho. O treino aceita
  isso, porque descartar maio a julho deixaria só agosto. Setembro e outubro têm a janela inteira, então a
  validação e o teste medem o caso de produção. O relatório registra esse limite.
- Antes, o `TunedThresholdClassifierCV` era previsto sobre "meses 1 a 4, validação no 5". A grade com dobras por
  mês é a forma de escolher hiperparâmetros sem tocar setembro.

### Regra de escolha (escrita antes de rodar)

1. **Métrica principal:** PR-AUC (*average precision*) na **validação** de setembro.
2. **LightGBM contra regressão logística:** *bootstrap* pareado com 2.000 reamostras e semente fixa. O LightGBM é
   escolhido quando o intervalo de 95% da diferença `PR-AUC(LightGBM) − PR-AUC(logística)` fica todo acima de zero.
   Senão, fica a regressão logística (princípio 3 de `ml-e-avaliacao.md`).
3. **Contra a regra de negócio:** o modelo escolhido precisa ter PR-AUC acima da regra "3 ou mais reclamações em
   30 dias", pelo mesmo critério. Se não tiver, a change para e o caso volta para análise.
4. **Calibração:** sigmoide com `CalibratedClassifierCV(FrozenEstimator(modelo))` em setembro. A isotônica não entra:
   são cerca de 70 positivos, longe dos ~1.000 da regra.
5. **Teste:** calculado uma vez, depois da escolha, para todos os modelos, só para o relatório.

**Se a regressão logística vencer,** o adaptador da aplicação é o `LogisticFraudScorer`: os coeficientes e a
padronização ficam em JSON, e a contribuição de cada feature é `coeficiente × valor padronizado` (soma exata com o
intercepto). Nesse caso a parte de `pred_contrib` não é escrita. Só o vencedor ganha adaptador, para não manter
código morto. O que a spec exige vale para os dois.

### Hiperparâmetros e determinismo do LightGBM

- **Grade pequena** (cerca de 12 combinações) de `num_leaves`, `min_child_samples`, `learning_rate` com
  `n_estimators`, e `reg_lambda`, escolhida pela PR-AUC média das três dobras.
- **Desbalanceamento:** sem `scale_pos_weight`. A calibração e a decisão por custo precisam da probabilidade, e
  repesar classes só a distorce antes de o calibrador consertar.
- **Determinismo:** `deterministic=True`, `force_col_wise=True`, `n_jobs=1` e `seed` do YAML. A mesma semente dá o
  mesmo modelo, e um teste da pesquisa confere isso num subconjunto.

### Custo e as regras comparadas

- **Custo esperado por reclamação:**
  - revisada: R$ 4,00, o custo da revisão (o revisor pega a fraude);
  - fraude não revisada: o valor pedido;
  - honesta não revisada: zero.
- **Regras comparadas no teste:**
  - nunca revisar;
  - revisar tudo;
  - a regra de negócio;
  - o limiar fixo do `TunedThresholdClassifierCV` (sobre o estimador congelado, ajustado em setembro, com um
    *scorer* de custo que recebe o valor por *metadata routing*);
  - a regra por custo: `p × valor ≥ custo`.
- **O custo da revisão** (400 centavos) é uma premissa explícita. Ele vai para o `decision.json` do artefato; a
  aplicação não tem esse número no código.
- A API do `TunedThresholdClassifierCV` e do *metadata routing* é conferida na documentação da versão instalada.

### Artefato publicado

Repositório de modelo `G0m7s/deliverius-fraud`, com uma tag por versão (`v1.0.0`):

| Arquivo | Conteúdo |
| --- | --- |
| `model.txt` | Booster do LightGBM em texto (`save_model`), ou `logistic.json` se a logística vencer |
| `calibrator.json` | `{"method": "sigmoid", "a": …, "b": …}`: `p = 1 / (1 + exp(a·p_bruta + b))`, como o `_SigmoidCalibration` |
| `decision.json` | `{"review_cost_cents": 400}` |
| `manifest.json` | versão do modelo, dataset (versão, repositório, commit), execução do MLflow, commit do código, métricas de validação e teste, `feature_schema_hash`, `features_version`, SHA-256 e tamanho de cada arquivo |
| `README.md` | model card em inglês (como o *dataset card*): dados sintéticos, métricas, limites, usos não pretendidos |

- **Sem `pickle` nem `joblib`:** carregar um pickle executa código de quem o escreveu. Texto e JSON só carregam
  números.
- **O calibrador é aplicado pela aplicação sem scikit-learn,** com os dois números. Um teste da pesquisa confere que
  dá o mesmo resultado do `CalibratedClassifierCV`, até 1e-9, em todo o teste. O scikit-learn usa
  `predict_proba` como entrada do calibrador do LGBM, porque o LGBM não tem `decision_function`; isso é conferido
  na versão instalada.

### Carga conferida, em dois passos

1. **Manifesto:** `fraud/model_files.py` fixa `REPOSITORY`, `REVISION` (o commit da tag) e o `ModelFile` do
   `manifest.json`, com tamanho e SHA-256.
2. **Arquivos:** lido o manifesto, cada arquivo listado vira um `ModelFile` e é conferido pelo mesmo
   `artifacts.verify_files`. Depois disso, o `feature_schema_hash` é comparado com o `schema_hash()` do código. Se
   não bater, sai `FraudModelError("fraud_model.schema_mismatch")`.

Trocar de modelo é um PR que muda `REVISION` e o hash do manifesto, com a comparação de métricas. A pasta local vem
de `FRAUD_MODEL_DIR` (padrão `models/fraud`), no `config.py` e no `.env.example`.

**Por que o pino fica no código, e não numa variável:** é como na busca (change 8). Uma variável deixaria trocar o
modelo sem PR e sem porta do CI.

### `artifacts`: o segundo caso real

O `retrieval/model_files.py` já resolve revisão fixa, conferência e download sem arquivo parcial. O antifraude é o
segundo uso, e o classificador será o terceiro. Por isso o código vai para `deliverius.artifacts`, que fica na
camada de utilitários, sem domínio, com `repo_type` (`model` ou `dataset`). A busca continua com as mesmas funções
públicas.

- **Eventos de log:** passam a ser `model_files.verified`, `model_files.rejected` e `model_files.download_failed`,
  com o campo `repository`. Isso troca os nomes `retrieval.model_*`, uma mudança não aditiva aceita porque nada
  consome esses eventos ainda (a observabilidade é a change 14). Fica registrado em `decisoes-arquitetura.md`.

### Pontuação

```
score(context):
  vector  = compute_features(context)                     # puro
  raw, contributions = booster.predict([vector], raw_score=True), booster.predict([vector], pred_contrib=True)
  p_raw   = sigmoid(raw); p = calibrate(p_raw)
  return FraudScore(probability=p, model_version, review=needs_review(p, amount, review_cost),
                    factors=top_factors(contributions, 3))
```

- **CPU fora do *event loop*:** a pontuação roda em `asyncio.to_thread`, com uma `threading.Lock` por *scorer* e
  `num_threads=1` na predição. Uma linha leva menos de 1 ms; a trava evita depender da segurança de threads do
  Booster.
- **Fatores:** são as contribuições no espaço bruto (log-odds) antes da calibração. A sigmoide é monótona, então a
  ordem não muda. Saem os três maiores em valor absoluto, com o sinal; o termo de base fica de fora.
- **Teste de arquitetura:** a regra do `.predict(` continua valendo para todo o `src/`. A exceção é uma lista
  explícita, `ALLOWED_SYNC_PREDICT = {"deliverius/fraud/scorer.py"}`, porque ali `predict` é um modelo de CPU, não
  um modelo de chat, e roda em thread. Um teste confere que cada entrada da lista existe; outro confere que
  `invoke`, `stream` e os demais continuam proibidos até nesses arquivos.
- **Falha ao pontuar:** sai `FraudScoringError`. A change 12 trata a falha mandando o caso para revisão humana
  (*fail closed*), e isso fica anotado no roadmap.

### Leitor do contexto

`PostgresFraudContextReader.history(scope: CustomerScope, claimed_at) → CustomerClaimHistory`, na sessão de quem
chama, sem `commit`. Faz três consultas, todas com `sandbox_id` e `customer_id` no `WHERE`:

1. **`customers`:** `created_at` pela chave primária.
2. **`orders`:** `placed_at` em `[claimed_at − 90 d, claimed_at)`, `ORDER BY placed_at DESC LIMIT 1000`, pelo
   índice existente `ix_orders_customer_listing`.
3. **Reclamações:** `approval_requests` por `claimed_at` na mesma janela `UNION ALL` `actions` com
   `approval_request_id IS NULL` por `executed_at`, `ORDER BY` instante `DESC LIMIT 1000`.
   - As `actions` usam o índice existente `ix_actions_sandbox_id_customer_id_executed_at`.
   - As `approval_requests` ganham o índice novo `(sandbox_id, customer_id, claimed_at)` na migration `0006`, com
     `CREATE INDEX CONCURRENTLY` dentro de `autocommit_block`.
   - O índice antigo `ix_approval_requests_customer_id` fica, porque cobre a FK.

O pedido atual vem do `orders.OrderReader`, que o agente já leu. Por isso `claim_context(order, proposal, claimed_at,
history)` é uma função pura, e o mesmo código monta o contexto do treino a partir do JSONL. Os planos são conferidos
com `EXPLAIN` no teste de integração.

### CLI e porta do CI

- **`python -m deliverius.cli fraud fetch-model`:** baixa e confere o modelo (sai com 2 se o hub falhar), igual ao
  `retrieval fetch-model`.
- **`python -m research.fraud.gate`:**
  1. baixa o dataset por `artifacts`, no commit da tag `v1.0.0`, com o manifesto de SHA-256 fixado no `config.yaml`,
     e confere tudo por `verify_dataset`;
  2. monta as features de outubro com o código atual;
  3. pontua com o `LightGbmFraudScorer` real;
  4. falha se a PR-AUC ficar abaixo do piso ou o Brier acima do teto.
- **Pisos:** o valor medido no teste menos 0,01 de PR-AUC, e mais 0,005 de Brier. A pontuação é determinística, então
  a margem só cobre diferença de plataforma.
- **No CI:** `uv sync --locked --group research` no job `quality`, cache de `models/fraud` (chave com a revisão) e
  de `data/generated/v1.0.0` (chave com o commit do dataset). Um teste confere que as chaves batem com as
  constantes, como no e5.

### Dependências

| Grupo | Pacotes | Por quê |
| --- | --- | --- |
| aplicação | `lightgbm` | o Booster e o `pred_contrib`; sem scikit-learn na imagem |
| `research` (fora de `default-groups` e da imagem) | `pandas`, `pandas-stubs`, `scikit-learn`, `shap`, `mlflow`, `matplotlib`, `pyyaml` | `ml-e-avaliacao.md` 1.7; o MLflow é pesado e só serve à pesquisa |

- **Na imagem,** o LightGBM precisa do OpenMP (`libgomp1`), instalado no estágio final do `Dockerfile`.
- **Tipos:** o `mypy` passa a cobrir `research/`, com `ignore_missing_imports` para os pacotes sem tipos
  (scikit-learn, shap, mlflow). Se o `lightgbm` não trouxer `py.typed`, ele entra nos *overrides* e fica atrás de
  um `Protocol` em `scorer.py`, como o `onnxruntime`.
- **Versões:** as versões e a API (`pred_contrib`, `FrozenEstimator`, `TunedThresholdClassifierCV`) são conferidas na
  documentação da versão instalada.

### Contratos do import-linter

- `deliverius.fraud` entra na camada de capacidades (`llm | retrieval | actions | approvals | fraud`) e não importa
  `actions` nem `approvals`. Ele lê as tabelas por `db.tables`, como os outros repositórios.
- **Contrato novo:** `lightgbm` só em `deliverius.fraud`.
- `deliverius.artifacts` entra nos utilitários: não importa domínio.
- `fraud` entra na lista "todo módulo menos `llm`", que proíbe provedores e `langchain_core`.
- `fraud` e `artifacts` entram na lista que proíbe `onnxruntime` e `tokenizers` fora de `retrieval`.

### Checklist de `robustez.md`, seção 11

- **Autorização:** não há rota nem ferramenta nova, então não há linha nova na matriz. O leitor recebe
  `CustomerScope` e filtra sandbox e cliente no `WHERE`, com teste de integração para "outro cliente" e "outra
  sandbox".
- **Corrida:** não se aplica. Não há escrita: a pontuação é pura, e o leitor só lê. O par "pontuar e depois criar a
  aprovação" é do agente (change 12), e a aprovação já é idempotente (change 11).
- **Idempotência:**
  - a pontuação é determinística;
  - o `fetch-model` não baixa o que já confere;
  - a publicação recusa uma tag existente;
  - o treino com a mesma semente reproduz o modelo.
- **Timeout:**
  - o download usa o cliente do hub com `HF_HUB_DOWNLOAD_TIMEOUT` (padrão da biblioteca), sem nova tentativa
    própria; o comando é rodado de novo;
  - as consultas usam o `statement_timeout` de 5 s, já aplicado a toda conexão;
  - não há rede na pontuação.
- **Limite:**
  - 1.000 linhas por consulta, com saturação igual no treino;
  - um vetor por chamada;
  - o modelo carregado uma vez, e a memória cabe em poucos MB.
- **Falha no meio:**
  - o download apaga o arquivo parcial (já é assim);
  - na publicação, uma queda entre o envio e a tag deixa um commit sem tag, e repetir envia de novo e cria a tag;
  - o treino só grava o artefato no fim, numa pasta temporária renomeada.
- **Transação com rede ou LLM:** não há. O leitor roda na sessão de quem chama, e a pontuação não toca o banco.
- **Eventos de log:**
  - `fraud.scored` (versão, probabilidade, revisão indicada, tipo de ação, latência em ms, ID do pedido);
  - `fraud.model_loaded` e `fraud.model_rejected` (regra, e os hashes no caso do esquema);
  - `model_files.*`.

  A taxa de revisão e a distribuição da nota viram métrica no painel (extra E1). Nenhum evento leva dado pessoal
  nem o valor das features de histórico.
- **Entrada não confiável:**
  - a proposta vem do LLM, mas chega aqui já validada pela política, e só o valor e os enums entram nas features;
  - nenhum texto livre entra;
  - os arquivos do modelo são conferidos por hash antes de serem lidos, e nenhum é *pickle*;
  - não há regex nova.
- **Compatibilidade:**
  - o banco ganha só um índice (aditivo);
  - a API e o grafo não mudam;
  - os eventos `retrieval.model_*` mudam de nome, sem consumidor ainda (registrado);
  - o `FraudAssessment` da change 11 não muda.
- **Banco:**
  - índice para cada filtro, com o plano conferido por `EXPLAIN` no teste de integração;
  - `LIMIT` em toda consulta;
  - sem N+1;
  - nenhuma FK nova;
  - a migration cria o índice com `CONCURRENTLY`, sem trava longa.
- **Travas:** não há trava nova.
- **Configuração:** `FRAUD_MODEL_DIR` tem padrão. A pasta sem o modelo ou com um modelo errado faz
  `load_fraud_model` falhar com a regra e o arquivo; a subida da API que o carrega é da change 12.

### Como o modelo é avaliado (regra do `config.yaml` do OpenSpec)

- **Métricas:**
  - PR-AUC, com intervalo de 95% por *bootstrap*;
  - ROC-AUC;
  - Brier e curva de confiabilidade (10 faixas por quantil);
  - recall nos 5% mais suspeitos;
  - custo esperado por regra.
- **Conjuntos:** validação de setembro (escolha) e teste de outubro (relatório e porta).
- **Limiares:** a regra de escolha acima, e os pisos do CI tirados do teste.
- **Relatório:** `docs/antifraude.md`, com as tabelas, os gráficos (confiabilidade, PR, SHAP global da biblioteca
  `shap`) e os limites.

## Risks / Trade-offs

- **Poucos positivos** (cerca de 70 na validação e 60 no teste) → os intervalos ficam largos, e o LightGBM pode não
  vencer a logística. A regra já está escrita, e o relatório mostra os intervalos; um resultado "a logística basta"
  também é uma resposta.
- **Dados sintéticos** → o modelo aprende o gerador, não a fraude real. O card diz isso, e os usos não pretendidos
  incluem decidir sobre clientes reais.
- **Janelas incompletas em maio a julho** → o treino vê históricos mais curtos que os de produção. A validação e o
  teste não têm esse problema, e o relatório registra o limite.
- **Diferença de instante na ação automática** (`executed_at` em vez de `claimed_at`) → poucos segundos, que só
  importam na borda das janelas de 30 e 90 dias.
- **Ovo e galinha da porta do CI:** o CI só pode medir depois de o modelo estar publicado. Até lá, os testes usam um
  Booster minúsculo treinado no próprio teste, e a porta entra no mesmo PR que fixa a revisão.
- **Publicação externa:** enviar ao Hugging Face depende da autorização do autor. Sem ela, a change fica pronta até
  a publicação, como na change 6.
- **Mudança de nome de evento** (`retrieval.model_*`) → aceita, porque ainda não há painel nem alerta; fica
  registrada.
