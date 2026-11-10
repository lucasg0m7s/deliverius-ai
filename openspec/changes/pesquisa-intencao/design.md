## Context

Motivação e escopo em `proposal.md`. O que já existe e molda o desenho:

- **Dados** (`data/generated/v1.0.0`, publicado em `G0m7s/deliverius-ai`, tag `v1.0.0`): `messages/` com treino
  (2.089), validação (440) e teste (440), divididos por cenário e estratificados por intenção; `messages_holdout/test`
  (550, outro gerador, Groq); 11 intenções equilibradas. Cada registro traz `text`, `intent`, `scenario_id`,
  `has_injection` e `pii_spans` (dados pessoais falsos do Faker no texto).
- **Teste escrito por pessoas:** só o formato e o validador existem (`datagen/human_test.py`, comando
  `datagen validate-human`: até 1 MB, 2.000 linhas, 600 caracteres, recusa dado pessoal). O arquivo ainda não foi
  escrito.
- **Mascaramento:** `privacy.mask_text(text, ledger, key)` normaliza o texto e troca dado pessoal por `[cpf_1]`,
  `[telefone_1]` etc. O texto de saída não depende da chave (a chave só entra nas impressões digitais do ledger).
- **e5-small em ONNX** já fixado e conferido (`retrieval/embedder.py`, `E5Embedder.embed_queries` com o prefixo
  `query: `), rodando sem torch.
- **Camada de LLM:** papéis com prazo e teto em `llm/roles.py`, orçamento diário por provedor (Gemini: 400 chamadas
  por dia), cache SQLite de respostas, `call_structured`, preço de tabela em `llm/pricing.py`, prompts versionados
  com `load_prompt`.
- **Pesquisa do antifraude** (`research/fraud/`): configuração em YAML com o dataset fixado arquivo por arquivo,
  candidato gravado em pasta, MLflow local (`mlruns/`, SQLite), publicação com ensaio, `--confirm`, tag e retomada.
- **Sem GPU local.** O ajuste fino do BERTimbau precisa do Kaggle.

## Goals / Non-Goals

**Goals:**

- Comparar as quatro abordagens com o mesmo código de avaliação, na mesma máquina, nos mesmos textos, e escolher pela
  regra escrita aqui, antes de qualquer número de teste.
- Treinar e avaliar sobre exatamente a entrada que a aplicação dará ao classificador.
- Deixar o vencedor publicado num formato que a change 9 carregue sem torch e sem *pickle*.
- Tudo por comando, reproduzível a partir do commit e da revisão do dataset.

**Non-Goals:**

- Servir o modelo, a rede de segurança por palavras-chave e a porta do CI (change 9).
- Otimizar hiperparâmetros a fundo: grades pequenas, escolhidas na validação.
- Medir a abordagem 4 em todo o teste sintético.

## Decisions

### D1. A entrada de todo modelo é a saída de `privacy.mask_text`

Cada mensagem (treino, validação e os três testes) passa por `mask_text(text, MarkerLedger.empty(<id>), key)`, com
uma chave fixa de pesquisa, antes de qualquer abordagem. É a primeira mensagem de uma conversa, como na aplicação.

- **Por quê:** na aplicação, `classify_intent` vem depois de `mask_input`. Treinar com `CPF 123...` e servir com
  `CPF [cpf_1]` seria *training-serving skew*. E, na abordagem 4, nenhum documento, telefone, e-mail ou endereço vai ao
  provedor.
- **Nomes próprios (medido na implementação):** o `mask_text` não reconhece nomes (`seguranca.md`, recall 0%, limite
  documentado). No dataset, os 340 nomes dos `pii_spans` sobram no texto mascarado; CPF, telefone, e-mail e endereço
  somem todos. A pesquisa mantém a paridade com a aplicação: os nomes falsos do Faker ficam no texto de treino e de
  teste e chegam ao LLM da abordagem 4, exatamente como o nome que um cliente escreve chega ao LLM do agente. Tirar os
  nomes só na pesquisa (pelos `pii_spans`) criaria a diferença entre treino e serviço que esta decisão evita. O teste
  confere que nenhum valor dos tipos cobertos sobra e conta os nomes como o limite conhecido.
- **Alternativas:** texto cru (diferença entre treino e serviço; dado pessoal falso no LLM); `redact_text` (marcadores
  sem número, diferentes dos da aplicação).
- O manifesto publicado declara o contrato de entrada (`input: privacy.mask_text`), e a change 9 o confere.

### D2. Regra de escolha (escrita antes dos resultados; refina `ml-e-avaliacao.md` 1.3)

1. **Elegível** é a abordagem com recall de `sensitive` ≥ 0,95 **nos três testes somados** (cerca de 105 mensagens
   sensíveis; só o teste humano teria cerca de 14, e um erro já derrubaria a regra) **e** p95 ≤ 100 ms em CPU.
2. Entre as elegíveis, a **melhor** é a de maior F1 macro no **teste escrito por pessoas**.
3. Para cada outra elegível, calcula-se o intervalo de 95% da diferença de F1 macro (melhor − outra) por *bootstrap*
   pareado e estratificado no teste humano (2.000 reamostragens, semente fixa). Se o intervalo contém zero, a
   diferença não é significativa. **Vence a mais rápida (menor p95) entre a melhor e as que empatam com ela.** O
   tamanho em disco desempata a latência.
4. **Nenhuma elegível:** nada é publicado; o relatório mostra por quê, e a decisão volta ao autor.

- **Por quê o *bootstrap* pareado:** dois intervalos que se sobrepõem não dizem que a diferença é nula (é um teste
  conservador demais); a diferença pareada usa os mesmos exemplos nos dois modelos. É o que a change 10 usou.
- **Consequência prevista:** a abordagem 4 não passa no p95 (rede + LLM, centenas de ms). Ela entra na tabela como a
  referência de "só perguntar ao LLM", não como candidata. Escrito aqui para não parecer escolha depois.

### D3. Três testes lidos uma vez; tudo o mais na validação

Hiperparâmetros, temperatura e limiar do roteador usam só `messages/validation`. A avaliação final roda uma vez, com
os quatro candidatos congelados em `models/intent-candidate/`, e lê os três testes. O teste humano tem o SHA-256
registrado no MLflow, no relatório e no manifesto. Um teste confere que nenhum texto do teste humano (normalizado)
aparece no treino ou na validação.

### D4. As abordagens

| # | Treino | Escolhido na validação | Saída |
| --- | --- | --- | --- |
| 1 | TF-IDF de palavras (1–2) e de caracteres dentro da palavra (3–5), `sublinear_tf`, `min_df=2`; regressão logística multinomial | `C` em {0,3; 1; 3; 10} | logits |
| 2 | `E5Embedder.embed_queries` da aplicação (o mesmo ONNX fixado) + regressão logística | `C` em {0,3; 1; 3; 10} | logits |
| 3 | `neuralmind/bert-base-portuguese-cased` com cabeça de classificação, no Kaggle | taxa em {2e-5; 3e-5; 5e-5} com uma semente; depois 3 sementes na taxa escolhida, se a cota permitir | logits |
| 4 | Gemini Flash-Lite, poucos exemplos no prompt | nada (prompt `v1` fixo) | intenção e confiança declarada |

- **BERTimbau:** até 4 épocas com parada antecipada pelo F1 macro da validação (paciência 1), lote 16, aquecimento de
  10%, *weight decay* 0,01, fp16 na GPU. Sequência máxima de 128 tokens, **a menos que** mais de 5% das mensagens da
  validação passem disso; então 256 (regra fixada agora, medida antes do treino). Exporta-se a semente de maior F1 na
  validação; o relatório dá média e desvio das sementes na validação.
- **Exportação local, em CPU:** `torch.onnx.export` exporta o *checkpoint* para ONNX, e
  `onnxruntime.quantization.quantize_dynamic` (o onnxruntime já é dependência da aplicação) quantiza em int8 dinâmico
  com pesos **uint8 por canal**, a escolha que a documentação do onnxruntime indica para CPUs AVX2 sem VNNI (evita a
  saturação do U8S8), não a AVX-512 VNNI (o ONNX int8 publicado do e5 exigia VNNI e não roda em toda máquina,
  decisão de 2026-11-08). Conferências: o ONNX fp32 bate com o PyTorch nos logits (1e-4) e o int8 concorda no argmax
  em ≥ 99% da validação; a queda de F1 do int8 vai para o relatório. As APIs foram conferidas nas versões
  instaladas (torch 2.15, transformers 5.22, onnxruntime 1.31).
- **Sem `optimum` (mudança feita na implementação):** todas as versões do `optimum-onnx` prendem
  `transformers < 4.58`, que exige `tokenizers ≤ 0.23.0`, e a aplicação exige `tokenizers ≥ 0.23.3`; o `uv` não
  resolve os dois no mesmo projeto. A exportação e a quantização direto pelo torch e pelo onnxruntime fazem o mesmo
  com uma dependência a menos.
- **Calibração:** *temperature scaling* nas abordagens 1 a 3, ajustada por log-verossimilhança na validação (busca
  escalar limitada). A abordagem 4 usa a confiança que o LLM declara; o ECE dela mostra exatamente quanto isso vale.
- **ECE:** 15 faixas de largura igual sobre a confiança da classe prevista.
- **Limiar do roteador:** na validação, o menor limiar `t` tal que o erro entre as mensagens com confiança ≥ `t` fica
  ≤ 5%. O relatório mostra a curva de cobertura × erro nos quatro conjuntos e a cobertura e o erro com `t` em cada
  teste.

### D5. Abordagem 4: amostra, papel próprio, entrada não confiável

- **Amostra fixa:** o teste humano inteiro e 20 mensagens por intenção de cada teste sintético (sorteio estratificado
  com semente; os IDs ficam no candidato). São cerca de 590 chamadas, dois dias da cota de 400. As abordagens 1 a 3
  também são medidas nessa amostra, para a comparação lado a lado.
- **Papel `intent_baseline`** em `llm/roles.py`: temperatura 0, 10 s por tentativa, prazo total de 25 s, 128 tokens de
  saída, modelo em `LLM_INTENT_BASELINE_MODEL` (padrão Gemini Flash-Lite). **Sem modelo de reserva:** uma resposta do
  Groq no meio misturaria dois modelos na mesma medida. Usar o `writer` mudaria a temperatura e o prazo da aplicação.
- **Retomável:** o cache SQLite de respostas da camada de LLM guarda cada resposta. Quando o orçamento do dia acaba, o
  comando para, diz quantas faltam e sai com código 2; no dia seguinte, o mesmo comando continua. A latência só é
  medida em chamada não cacheada e fica gravada por ID de mensagem, para a retomada não perdê-la.
- **Prompt `v1`** em `research/intent/prompts/intent_baseline/v1.md`: as 11 intenções descritas em português e 2
  exemplos de treino por intenção (sorteio com semente, já mascarados). A mensagem do cliente vai só na mensagem do
  usuário, num bloco `<mensagem>` com `<`, `>` e `&` escapados, nunca no prompt de sistema (`seguranca.md` 1.3).
- **Saída estruturada** (`call_structured`): `intent` restrita às 11 classes e `confidence` em [0, 1]. Resposta
  inválida, recusa, injeção que funcionou ou modo degradado contam como **erro** (previsão vazia, confiança 0), nunca
  são descartadas; o relatório conta quantas foram. Nenhuma decisão depende de o LLM obedecer: é só medição.
- **Custo:** tokens de entrada e saída de cada chamada vezes o preço de tabela de `llm/pricing.py`, em custo por mil
  mensagens. O relatório diz que, na cota gratuita, o custo cobrado é zero.

### D6. Medição de latência e tamanho

Uma mensagem por vez, num processo, em CPU, do texto mascarado às probabilidades (vetorização ou tokenização
incluídas). `onnxruntime` com 1 thread de intra-op, o padrão de `RETRIEVAL_THREADS` da aplicação. 20 mensagens de
aquecimento fora da conta, depois todas as mensagens dos três testes. O relatório diz qual CPU. Tamanho = soma dos
arquivos que seriam publicados (na abordagem 2, inclui o e5).

### D7. Kaggle: o notebook só chama o script

- `research/intent/finetune.py` (configuração em `research/intent/config.yaml`) treina, avalia na validação e grava o
  *checkpoint* (safetensors), o tokenizador e o registro das épocas. O notebook
  `research/intent/notebooks/finetune_bertimbau.ipynb` clona o repositório público num commit fixo, instala o grupo
  `finetune`, baixa o dataset do Hub na revisão fixa (conferindo os SHA-256) e chama o script.
- O teste humano nunca vai ao Kaggle: o treino não precisa dele.
- O *checkpoint* volta como saída do notebook; a exportação, a calibração e a avaliação oficial rodam localmente
  (`ml-e-avaliacao.md`, princípio 2).
- **Alternativa:** treino escrito nas células do notebook (não reproduzível por comando, princípio de 1.2); Colab
  (sem GPU garantida).

### D8. Grupos de dependências

- `research` (já existe) ganha só o necessário para as abordagens 1, 2 e 4 (scikit-learn, MLflow, matplotlib já
  estão lá).
- **Grupo novo `finetune`:** torch, transformers, accelerate, datasets e onnx (a quantização usa o onnxruntime da
  aplicação). Fora de
  `default-groups` e fora do CI; localmente, só para exportar. Testes que precisam dele levam o marcador novo
  `finetune`, fora do `pytest` padrão. As partes puras do `finetune.py` (configuração, mapa de rótulos, regra dos 128
  ou 256 tokens) não importam torch e rodam no CI.
- **Alternativa:** tudo no `research` (o CI baixaria torch, mais de 1 GB, para nada).

### D9. Peças comuns da pesquisa

- `research/dataset.py` + `research/dataset.yaml`: a fixação do dataset `v1.0.0` (revisão, tamanho e SHA-256 de cada
  arquivo), hoje na seção `dataset` de `research/fraud/config.yaml`. O antifraude e a intenção leem dali.
- `research/hub.py`: `ModelHub`, `HuggingFaceModelHub`, `PublishError` e o envio com tag e retomada, hoje em
  `research/fraud/publish.py`.
- Comportamento do antifraude igual; os testes do antifraude passam sem mudança de asserção. É o segundo caso real
  que justifica a abstração (`convencoes-codigo.md`).

### D10. Formato publicado, por vencedor

- **BERTimbau:** `model.onnx` (int8), `tokenizer.json`, `labels.json`, `calibration.json` (temperatura e limiar).
- **TF-IDF + logística:** `tfidf.json` (vocabulário, IDF, parâmetros do analisador) e `logistic.json` (coeficientes).
  Uma implementação de referência em numpy (`research/intent/portable.py`) lê os JSON e tem de bater com o
  scikit-learn até 1e-9 nas probabilidades da validação; é o que a change 9 copiará.
- **e5 + logística:** `logistic.json`; o e5 é o da aplicação, e o manifesto aponta a revisão dele.
- Sempre: `manifest.json` (versão do modelo, abordagem, versão e revisão do dataset, SHA-256 do teste humano, ID da
  execução no MLflow, rótulos em ordem, temperatura, limiar, contrato de entrada, métricas por conjunto e SHA-256 de
  cada arquivo) e o card em inglês. Repositório `G0m7s/deliverius-intent`, tag `v1.0.0`; o ensaio é o padrão e
  `--confirm` envia.
- Só o formato do vencedor é implementado (como o adaptador da change 10).

### D11. Como é avaliado (resumo para o checklist de modelo)

| Métrica | Conjuntos | Limiar |
| --- | --- | --- |
| F1 macro com IC 95% (*bootstrap* estratificado, 2.000) | os três testes e a amostra da abordagem 4 | escolha pela regra D2 |
| Recall de `sensitive` | os três somados (elegibilidade) e cada um | ≥ 0,95 |
| p50/p95 de latência | todas as mensagens de teste | p95 ≤ 100 ms |
| ECE (15 faixas) | cada teste | informativo |
| Cobertura e erro com o limiar | cada teste | erro ≤ 5% na validação |
| Custo por mil mensagens, tamanho | — | desempate |

### Checklist de `robustez.md`, seção 11

- **Autorização:** não se aplica. Nenhuma rota, ferramenta ou consulta nova; tudo é comando local de pesquisa.
- **Corrida:** não se aplica. Um processo por comando e um comando da abordagem 4 por vez (o cache SQLite abre a
  conexão com espera de 5 s, mas duas execuções simultâneas só gastariam cota em dobro, sem estragar o resultado).
- **Idempotência:** o candidato é gravado numa pasta temporária e renomeado; repetir sobrescreve com o mesmo
  conteúdo. Repetir a abordagem 4 lê do cache, sem nova chamada. A publicação recusa tag existente e retoma o envio
  interrompido entre o envio e a tag (`research/hub.py`).
- **Timeout e novas tentativas:** LLM pelo papel `intent_baseline` (10 s por tentativa, 25 s no total, novas
  tentativas da camada só em erro passageiro); downloads pelo `artifacts` (`hf_hub_download`), com o timeout padrão
  do `huggingface_hub`, conferido na versão instalada; o Kaggle e o envio ao Hub são comandos que se repetem à mão.
- **Limites:** cota diária do Gemini pelo orçamento da camada (400); teto de 128 tokens de saída; teste humano com os
  limites do validador; amostra fixa da abordagem 4.
- **Queda no meio:** treino local não deixa candidato parcial (renomeação no fim); a abordagem 4 retoma pelo cache; o
  Kaggle perde só a execução, que é repetida.
- **Transação com rede ou LLM:** não se aplica. Não há banco.
- **Eventos de log:** `intent_research.dataset_verified`, `intent_research.human_test_verified` (linhas e SHA-256),
  `intent_research.candidate_written` (abordagem), `intent_research.llm_call_failed` (ID da mensagem e motivo, nunca o
  texto), `intent_research.budget_exhausted` (faltantes), `intent_research.evaluation_finished` e
  `intent_research.published`. Nenhum vira métrica.
- **Texto de fora:** o teste humano passa pelo validador antes de ser lido (sem regex nova); as mensagens sintéticas
  trazem injeções de propósito e vão ao LLM só mascaradas e delimitadas; a saída do LLM só é aceita pelo esquema
  estruturado.
- **Grafo, banco e API:** não mexe. O papel novo na camada de LLM é aditivo.
- **Consultas e índices, travas e migrations:** não se aplica.
- **Configuração nova:** `LLM_INTENT_BASELINE_MODEL`, com padrão; a chave do Gemini só é exigida pelo comando da
  abordagem 4, ao montar o papel, com mensagem clara (já é o comportamento de `LlmRuntime.build`).

### Checklist de `seguranca.md`, seção 7 (conferido na implementação)

- **Texto do cliente sem mascaramento em log, banco ou LLM:** não. Todo texto passa por `mask_text` antes de qualquer
  abordagem (D1); o evento `intent_research.llm_call_failed` leva só o ID e o motivo; os erros comentados do teste
  humano ficam em `models/intent-candidate/evaluation.json` e no MLflow local, fora do git. Nomes próprios sobram,
  como na aplicação (D1).
- **Ferramenta do agente, ação com efeito, rota nova:** não se aplica.
- **Dado de saúde num LLM externo:** sim, de propósito e só dado inventado. As mensagens `sensitive` sintéticas e as
  do teste humano (escritas pelo autor, sem pessoa real, conferidas pelo validador) vão ao Gemini na abordagem 4,
  porque medir o LLM nelas é a pergunta da pesquisa. Na aplicação o caminho sensível continua indo para humano sem
  LLM; o papel `intent_baseline` só existe na pesquisa.
- **Artefato com *pickle* ou sem revisão fixa:** o candidato e o que se publica são JSON, ONNX e `tokenizer.json`,
  com SHA-256 em `candidate.json` e no manifesto. O `training_args.bin` (um *pickle*) que o `Trainer` grava é apagado
  da saída do ajuste fino, e um teste confere que nenhum `.bin`, `.pt` ou `.pkl` sobra. O BERTimbau base só existe no
  Hub como `pytorch_model.bin`: é lido no Kaggle, numa revisão fixa (`base_revision`), pelo transformers 5 com
  torch ≥ 2.6, que carrega com `weights_only` (sem executar código); a saída é safetensors.
- **Segredo no código:** não. A chave de mascaramento da pesquisa é derivada de um texto fixo e não é segredo (o texto
  mascarado não depende dela); o `HF_TOKEN` só é lido no `--confirm`.
- **Prompt, modelo ou grafo:** prompt novo, só da pesquisa; o grafo e os prompts da aplicação não mudam, então os
  evals de segurança não se aplicam.
- **Texto de fora no prompt:** só na mensagem do usuário, no bloco `<mensagem>` com `<`, `>` e `&` escapados (testado
  com `</mensagem>` dentro da mensagem); a resposta só é aceita pelo esquema (uma das 11 intenções e um número), sem
  texto livre, link ou imagem.
- **Checklist de `robustez.md`:** respondido acima.

## Risks / Trade-offs

- **[Teste humano pequeno, cerca de 14 por intenção] → intervalos largos.** Mitigação: o recall de `sensitive` usa os
  três testes somados; a regra D2 já trata o empate a favor do mais simples; o relatório mostra os intervalos.
- **[Um só autor escreve o teste humano] → viés de estilo.** Mitigação: as regras de escrita do `README` de
  `data/human-test` pedem variedade; mensagens de outras pessoas são bem-vindas; o limite fica no relatório.
- **[Validação sintética fácil] → limiar do roteador frouxo para texto real.** Mitigação: a curva de cobertura × erro
  no teste humano mostra a diferença; trocar o limiar fica para a change 9, com outra regra escrita antes.
- **[Kaggle] conta sem GPU liberada (exige telefone verificado) ou cota acabando.** Mitigação: uma semente basta para
  a comparação; Colab como reserva (`ml-e-avaliacao.md` 1.2).
- **[Perda na quantização int8].** Mitigação: conferência de concordância (≥ 99%) e queda de F1 no relatório; se
  falhar, publica-se o ONNX fp32 e o relatório diz o tamanho.
- **[Cota do Gemini dividida com o desenvolvimento].** Mitigação: amostra de 590 chamadas em dois dias, retomável.
- **[Mesmo gerador no treino e no teste sintético] → nota inflada.** É por isso que existem o teste de outro gerador e
  o humano; a regra escolhe pelo humano.

## Migration Plan

Nada a implantar: pesquisa local, sem banco e sem API. A ordem de execução, que o README registra como "pipeline":

1. Abordagens 1 e 2 e a amostra da abordagem 4 (sem o teste humano, só na validação).
2. Kaggle: busca da taxa e sementes; download do *checkpoint*; exportação e calibração locais.
3. O autor termina e valida o teste humano; ele entra no git.
4. Avaliação final única → escolha pela regra → relatório.
5. Com autorização: publicação no Hub e notebook público no Kaggle.

Reverter é apagar `research/intent/` e o papel `intent_baseline`; nada da aplicação depende disso até a change 9.

## Decisões humanas pendentes

1. **Escrever o teste por pessoas** (cerca de 150 mensagens, ao menos 10 por intenção) em
   `data/human-test/human_test.csv` e validá-lo com `datagen validate-human`. Bloqueia a etapa 4.
2. **Conta do Kaggle** com GPU liberada e a autorização para tornar o notebook público.
3. **Autorização para publicar** o modelo em `G0m7s/deliverius-intent`.
4. Se nenhuma abordagem for elegível pela regra D2: o que fazer.

## Open Questions

- Se a cota de GPU der para as 3 sementes do BERTimbau ou só para uma. Não muda a regra nem as tarefas: o relatório
  diz quantas rodaram.
