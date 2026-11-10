## 1. Preparação

- [x] 1.1 `pyproject.toml`: grupo `finetune` (torch, transformers, accelerate, datasets, onnx; sem `optimum`, ver design D4) fora de `default-groups`; marcador `finetune` fora do `pytest` padrão (como `integration` e `llm`); conferir na documentação das versões instaladas a exportação ONNX do torch e a quantização dinâmica do onnxruntime, o `Trainer` com parada antecipada e o `hf_hub_download` (timeout padrão); `uv sync --group research` e `uv sync --group finetune` passam
- [x] 1.2 `mypy` cobrindo `research/intent/` (`ignore_missing_imports` para torch, transformers, datasets, accelerate, onnx); import-linter: `research` nunca importado por `src/`, `torch`/`transformers`/`onnx` nunca em `src/`; `uv run mypy` e `uv run lint-imports` passam
- [x] 1.3 `llm/roles.py`: papel `intent_baseline` (temperatura 0, 10 s, 25 s, 128 tokens); `config.py`: `llm_intent_baseline_model` com padrão Gemini Flash-Lite e sem reserva; `LLM_INTENT_BASELINE_MODEL` no `.env.example`; testes da configuração do papel e do padrão em `test_config.py`

## 2. Peças comuns da pesquisa

- [x] 2.1 `research/dataset.yaml` + `research/dataset.py`: a fixação do dataset `v1.0.0` sai de `research/fraud/config.yaml`; o antifraude (treino, porta, CI) passa a ler dali; testes do antifraude passam sem mudar asserção; teste que mantém a chave de cache do CI igual à fixação
- [x] 2.2 `research/hub.py`: `ModelHub`, `HuggingFaceModelHub`, `PublishError` e o envio com tag e retomada saem de `research/fraud/publish.py`; testes do hub falso movidos (ensaio não envia, tag existente recusa, queda entre envio e tag é retomada)

## 3. Dados da pesquisa (`research/intent/`)

- [x] 3.1 `config.yaml` e `config.py` (semente, grades, amostra da abordagem 4, *bootstrap* de 2.000, faixas do ECE, alvo de 5% de erro do limiar, p95 de 100 ms, recall de 0,95), escritos antes de qualquer execução
- [x] 3.2 `data.py`: lê treino, validação, teste e holdout por `verify_dataset`/`read_records`; aplica `mask_text` com ledger vazio por mensagem e chave fixa; testes: dataset com hash errado é recusado; o texto mascarado é igual ao que `mask_text` dá na aplicação; um dado pessoal falso do registro (pelos `pii_spans`) dos tipos que o mascaramento cobre nunca sobra no texto de entrada; nomes contados como o limite conhecido (design D1)
- [x] 3.3 `human_test.py` da pesquisa: lê `data/human-test/human_test.csv` só depois de `validate_human_test` passar, exige ≥ 10 por intenção, devolve o SHA-256 do arquivo; teste de contaminação: nenhum texto do teste humano (normalizado) está no treino ou na validação; testes com arquivo inválido, intenção com menos de 10 e arquivo ausente
- [x] 3.4 `sample.py`: a amostra estratificada da abordagem 4 (20 por intenção de cada teste sintético + o teste humano inteiro), determinística pela semente; teste de determinismo e de contagem por intenção

## 4. Métricas e regra de escolha

- [x] 4.1 `metrics.py`: F1 macro com IC por *bootstrap* estratificado, recall por classe, ECE em 15 faixas, *temperature scaling* por log-verossimilhança, limiar do roteador (menor `t` com erro ≤ 5%), curva de cobertura × erro, p50/p95; previsão vazia conta como erro; testes com vetores pequenos de resultado conhecido
- [x] 4.2 `choice.py`: a regra D2 do design (elegibilidade nos três testes somados e p95; melhor pelo F1 humano; *bootstrap* pareado; a mais rápida entre as que empatam; tamanho desempata; nenhuma elegível → sem vencedor); testes de cada ramo: sensível abaixo de 0,95, p95 acima, empate a favor do mais rápido, diferença significativa mantém o melhor, ninguém elegível
- [x] 4.3 `candidate.py`: grava cada candidato em `models/intent-candidate/<abordagem>/` numa pasta temporária renomeada no fim, com a função de previsão, a temperatura, o limiar e os parâmetros; teste: queda antes da renomeação não deixa candidato parcial

## 5. Abordagens 1 e 2 (local, CPU)

- [x] 5.1 `tfidf.py`: TF-IDF de palavras e caracteres + regressão logística, `C` escolhido na validação; teste de determinismo num subconjunto
- [x] 5.2 `e5.py`: `E5Embedder.embed_queries` da aplicação (modelo de `RETRIEVAL_MODEL_DIR`) + regressão logística, `C` na validação; teste com um embedder falso
- [x] 5.3 `train.py`: comando que treina 1 e 2, calibra, escolhe o limiar na validação e registra no MLflow (experimento `deliverius-intent`: parâmetros, métricas da validação, versão do dataset, commit, semente); rodar e guardar os candidatos

## 6. Abordagem 4 (LLM)

- [x] 6.1 Prompt `research/intent/prompts/intent_baseline/v1.md` (cabeçalho do `load_prompt`): as 11 intenções em português e 2 exemplos mascarados por intenção sorteados do treino
- [x] 6.2 `llm_baseline.py`: monta a mensagem do usuário com o bloco `<mensagem>` escapado; `call_structured` com `intent` restrita às 11 classes e `confidence` em [0, 1]; falha, recusa ou degradado viram previsão vazia e o evento `intent_research.llm_call_failed` (sem o texto); latência gravada por ID só em chamada não cacheada; custo pelos tokens e `llm/pricing.py`; orçamento esgotado → para, mostra quantas faltam e sai com 2; testes com o LLM falso: a mensagem com `</mensagem>` e `<` fica escapada e fora do prompt de sistema, saída fora das classes conta como erro, retomada não chama de novo o que está no cache, latência preservada na retomada
- [x] 6.3 Rodar na amostra da **validação** para conferir o prompt (sem tocar os testes) e registrar no MLflow

## 7. Abordagem 3 (BERTimbau)

- [x] 7.1 `finetune.py`: partes puras sem torch (configuração, mapa de rótulos, a regra dos 128 ou 256 tokens medida na validação) testadas no CI; o laço com `Trainer`, parada antecipada pelo F1 macro da validação e gravação do *checkpoint* em safetensors; teste `finetune` com um modelo minúsculo aleatório num subconjunto
- [x] 7.2 Notebook `research/intent/notebooks/finetune_bertimbau.ipynb`: clona o repositório num commit fixo, instala o grupo `finetune`, baixa o dataset na revisão fixa conferindo os SHA-256 e chama o script; as células só chamam comandos
- [ ] 7.3 **Com a conta do autor no Kaggle:** busca da taxa (uma semente), depois 3 sementes na taxa escolhida se a cota der; baixar o *checkpoint* e o registro das épocas
- [x] 7.4 `export.py`: ONNX pelo `torch.onnx.export`, quantização int8 dinâmica do onnxruntime (pesos uint8 por canal); conferências: fp32 × PyTorch nos logits (1e-4) e int8 concorda no argmax em ≥ 99% da validação; temperatura e limiar na validação com o int8; registro no MLflow; teste `finetune` da exportação com o modelo minúsculo

## 8. Avaliação final (uma vez)

- [x] 8.1 **Depende do autor:** teste humano escrito, `datagen validate-human` sem problemas, arquivo no git. **Mudou em 2026-11-10, antes de qualquer resultado:** por decisão do autor, o arquivo tem 165 mensagens de clientes simulados (três agentes do Claude em personas, 5 por intenção cada), revisadas (uma mudou de rótulo); o relatório o chama de teste de clientes simulados e diz que nenhuma pessoa o escreveu
- [x] 8.2 `evaluate.py`: os quatro candidatos congelados, uma mensagem por vez, 1 thread de intra-op, 20 de aquecimento; F1 com IC, recall de `sensitive`, ECE, p50/p95, cobertura e erro com o limiar, custo por mil, tamanho, nos três testes e na amostra; matriz de confusão e curvas; aplica `choice.py`; registra tudo numa execução-mãe do MLflow com o SHA-256 do teste humano; teste do comando com candidatos falsos
- [ ] 8.3 Rodar a avaliação final (a abordagem 4 pode levar dois dias, retomando pelo cache) e registrar o vencedor
- [ ] 8.4 Se o vencedor for a abordagem 1: `portable.py` em numpy lê `tfidf.json` e `logistic.json` e bate com o scikit-learn até 1e-9 nas probabilidades da validação (teste); se for a 2: o mesmo para `logistic.json` sobre os embeddings

## 9. Publicação

- [x] 9.1 `research/intent/publish.py`: monta os arquivos do vencedor (D10), `manifest.json` (com o contrato de entrada `privacy.mask_text`, rótulos, temperatura, limiar, métricas, SHA-256 do teste humano e de cada arquivo) e o card em inglês; ensaio por padrão; `--confirm` envia por `research/hub.py`; testes com o hub falso
- [ ] 9.2 **Com a autorização do autor:** publicar `G0m7s/deliverius-intent` `v1.0.0` e anotar a revisão (a change 9 a fixa no código)
- [ ] 9.3 **Com a autorização do autor:** tornar o notebook público no Kaggle e anotar o link

## 10. Documentação

- [ ] 10.1 `docs/pesquisa-intencao.md`: hipótese, método, a regra (copiada antes dos resultados), a tabela dos três testes e da amostra com intervalos, recall de `sensitive`, ECE, latência, custo e tamanho, a curva de cobertura × erro e o limiar, a matriz de confusão, 20 erros comentados, a escolha pela regra, os limites (teste humano pequeno e de poucos autores, validação sintética, CPU usada) e a linhagem (commit, dataset, MLflow, revisão no Hub)
- [x] 10.2 `docs/ml-e-avaliacao.md` 1.3: entrada mascarada, regra com *bootstrap* pareado e elegibilidade nos três testes, ECE em 15 faixas, amostra da abordagem 4, grupo `finetune`
- [x] 10.3 `docs/decisoes-arquitetura.md`: entrada mascarada na pesquisa, regra pareada, papel `intent_baseline` sem reserva, amostra da abordagem 4, grupo `finetune`, int8 AVX2, peças comuns de `research/`, formato publicado por vencedor
- [x] 10.4 `docs/arquitetura.md` (`research/intent/`, papel novo) e `docs/roadmap.md` (seção 9: o adaptador segue o vencedor, com o formato do manifesto)
- [ ] 10.5 README: tabela resumida, links do modelo no Hugging Face e do notebook no Kaggle, comandos da pesquisa na ordem e a change 7 no status
- [x] 10.6 Conferir o checklist de `docs/seguranca.md`, seção 7 (nada de dado pessoal no LLM nem em log, texto do cliente delimitado, artefato sem *pickle* e conferido por hash)

## 11. Verificação

- [x] 11.1 `uv run ruff format --check .`
- [x] 11.2 `uv run ruff check .`
- [x] 11.3 `uv run mypy`
- [x] 11.4 `uv run lint-imports`
- [x] 11.5 `uv run pytest` (cobertura mínima de 85% e 100% dos ramos em `policy`, `privacy`, `actions` e `fraud.features`)
- [x] 11.6 `uv run pytest -m integration --no-cov` (nada de banco muda, mas a fixação do dataset do antifraude mudou de lugar)
- [x] 11.7 `uv run pytest -m finetune --no-cov` com o grupo `finetune`
- [x] 11.8 `uv run python -m research.fraud.gate` continua passando depois da extração das peças comuns
- [x] 11.9 `openspec validate pesquisa-intencao --strict`
