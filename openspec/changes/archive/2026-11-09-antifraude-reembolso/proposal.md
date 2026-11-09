## Why

Change 10 do `docs/roadmap.md` (`antifraude-reembolso`, fase F2), que cobre os requisitos **R03** (modelos em
operação), **R04** (treino de modelos), **R05** (pipelines reproduzíveis), **R06** (ML aberto: baselines,
calibração, SHAP), **R07** (modelos publicados) e **R24** (fundamentos de ML clássico). O motor de política (change 3)
sabe se uma ação é permitida, mas não se a reclamação é verdadeira. Por isso o nó `score_fraud` do agente (change 12)
precisa de uma nota calibrada e de uma regra de revisão humana baseada no custo. Os dados já estão prontos (dataset
`v1.0.0`, change 6) e o treino roda em CPU, então a change não depende de LLM nem de GPU.

**Ordem no roadmap:** a change 10 depende da 9 (`classificador-intencao`) só pela carga de artefatos do Hugging Face
com revisão fixa. A 9 depende da pesquisa de intenção (change 7), que ainda espera o teste escrito por pessoas. Por
isso a carga de artefatos é construída aqui, e a change 9 passa a reaproveitá-la; o roadmap é atualizado.

## What Changes

Escopo copiado do roadmap:

- `fraud/features.py`: um só código para o treino e para a aplicação, calculado no ponto do tempo (*point-in-time*).
- `research/fraud/`: divisão por tempo, LightGBM, calibração, regra de revisão por custo comparada com o
  `TunedThresholdClassifierCV`, e SHAP.
- O adaptador `FraudScorer` com `pred_contrib`.
- A publicação no Hugging Face com model card e manifesto.
- A regra do teste de arquitetura da change 5 que proíbe `.predict(` em `src/`: decidido aqui que ela vale só para
  modelos de chat, com uma lista explícita de módulos liberados (`fraud`).

Detalhado a partir do escopo (o roadmap foi atualizado com estes pontos):

- **Carga de artefatos com revisão fixa e SHA-256** (`deliverius.artifacts`): o código de `retrieval/model_files.py`
  vira genérico e passa a servir a busca, o antifraude e, depois, o classificador. O manifesto do modelo tem o
  SHA-256 fixado no código e lista o SHA-256 de cada arquivo. Se o hash do esquema de features do manifesto não bater
  com o do código, o modelo é recusado.
- **A reclamação, como a aplicação a enxerga:** as features usam só o que a aplicação também grava. Uma reclamação
  anterior é um pedido de aprovação, ou uma ação sem aprovação; a reclamação negada pela política não é gravada.
  Por isso a feature "reclamações recusadas antes", do plano, vira **"reclamações enviadas à revisão em 90 dias"**. O
  desfecho do revisor continua fora, porque entregaria o rótulo.
- **Leitor do contexto** (`FraudContextReader`): a porta e a implementação em Postgres, com `CustomerScope`, que
  montam a conta, os pedidos e as reclamações anteriores do cliente. Inclui o índice novo
  `approval_requests (sandbox_id, customer_id, claimed_at)` na migration `0006`.
- **Porta no CI:** PR-AUC mínima e Brier máximo do modelo publicado no teste (outubro), calculados com o código atual
  de features, a partir do dataset e do modelo em revisões fixas, com cache. Uma mudança de feature que estrague o
  modelo servido quebra o CI.
- Dependências: `lightgbm` na aplicação e um grupo `research` (pandas, scikit-learn, shap, mlflow, matplotlib) fora
  da imagem.

## Fora de escopo

- O dataset e o notebook no Kaggle (trilha de publicação).
- O nó `score_fraud` no grafo, o preenchimento de `FraudAssessment` na aprovação e o carregamento do modelo na
  inicialização da API (change 12). O `GET /ready` é da change 9.
- Gravar as reclamações negadas pela política. Se isso entrar no futuro, a feature de recusas volta, numa versão
  nova do esquema de features.
- Reavaliar a nota na execução (a política é reavaliada; a nota não).
- Monitoramento de deriva e retreino periódico.
- Isotônica como calibrador: o teste tem cerca de 60 fraudes e a validação cerca de 70; a regra de
  `ml-e-avaliacao.md` pede perto de mil positivos.

## Pronto quando

Copiado do roadmap:

- PR-AUC, Brier, curva de confiabilidade e custo esperado no teste, contra um baseline de regras;
- 100% de cobertura de ramos em `fraud/features`;
- um teste prova que nenhuma feature usa dado posterior à reclamação;
- PR-AUC e Brier viram porta no CI.

Somam-se a isso: o modelo publicado no Hugging Face numa revisão fixa, com card e manifesto, e o relatório em
`docs/antifraude.md`.

## Capabilities

### New Capabilities

- `antifraude`: nota de fraude calibrada para uma reclamação, com features no ponto do tempo compartilhadas entre o
  treino e a aplicação, a regra de revisão por custo, os três fatores que mais pesaram, a carga do modelo numa
  revisão fixa e conferida, e a porta de qualidade no CI.

### Modified Capabilities

Nenhuma. A troca de `retrieval/model_files.py` pelo módulo genérico mantém o comportamento exigido em
`busca-politicas` (revisão fixa e SHA-256 conferido); só os nomes dos eventos de log mudam, e ainda não há quem os
consuma (observabilidade é a change 14).

## Impact

- Código novo: `src/deliverius/fraud/` (features, modelos, porta, scorer LightGBM, leitor em Postgres, arquivos do
  modelo); `src/deliverius/artifacts/`; `research/fraud/` (treino, avaliação, publicação, porta do CI); comandos
  `fraud fetch-model` na CLI; migration `0006`.
- Código alterado: `retrieval/model_files.py` passa a usar `artifacts`; o teste de arquitetura das chamadas
  síncronas; contratos do import-linter (`fraud` na camada de capacidades; `lightgbm` só em `fraud`).
- `pyproject.toml`: `lightgbm` nas dependências, grupo `research`, `mypy` e `pytest` cobrindo `research/`.
- `Dockerfile`: `libgomp1` na imagem final (o LightGBM precisa do OpenMP).
- CI: porta de 100% de ramos em `fraud.features`, cache do dataset e do modelo, porta de PR-AUC e Brier.
- Docs: `ml-e-avaliacao.md` 1.4 (features ajustadas, população pontuada, divisão), `arquitetura.md`,
  `decisoes-arquitetura.md`, `roadmap.md`, `docs/antifraude.md` (relatório novo), README (números e link do modelo).
- Ação externa: publicar o modelo no Hugging Face (`G0m7s/deliverius-fraud`), que só acontece com a autorização do
  autor.
