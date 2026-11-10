## Why

Change 7 do `docs/roadmap.md` (`pesquisa-intencao`, fase F2), que cobre os requisitos **R04** (treino e ajuste fino),
**R05** (pipelines reproduzíveis), **R06** (ML aberto: baselines, validação, análise de erros, calibração), **R07**
(modelos publicados), **R08** (pesquisa com hipótese e método), **R09** (notebook público reproduzível) e **R24**
(fundamentos de ML clássico).

O nó `classify_intent` do agente decide o caminho de cada mensagem: sensível vai para humano sem LLM, dúvida vai para
a FAQ, problema com pedido vai para o agente, e confiança baixa vira pergunta. A pergunta da pesquisa é **"quanto
modelo isso precisa?"**: um TF-IDF resolve? Um embedding pronto basta? O ajuste fino do BERTimbau paga o custo? E
"só perguntar ao LLM", que não exige treino, quanto custa em latência e em dinheiro? A change 9
(`classificador-intencao`) serve o vencedor; ela depende desta.

É pesquisa: a regra de escolha fica escrita **antes** dos experimentos, como em `ml-e-avaliacao.md`, princípio 1. Por
isso esta proposta sai antes de existir o teste escrito por pessoas.

## What Changes

Escopo copiado do roadmap:

- `research/intent/` com as quatro abordagens do núcleo de `ml-e-avaliacao.md`, seção 1.3:
  1. TF-IDF + regressão logística;
  2. e5-small + regressão logística;
  3. BERTimbau com ajuste fino no Kaggle, exportado para ONNX int8;
  4. LLM sem treino.
- Todas avaliadas pelo mesmo script, local, em CPU, nos três conjuntos de teste, e registradas no MLflow.
- O vencedor publicado no Hugging Face com model card e `manifest.json`.
- O relatório em `docs/pesquisa-intencao.md` e o notebook de treino público no Kaggle.

Detalhado a partir do escopo (o roadmap foi atualizado com estes pontos):

- **A entrada é o texto mascarado.** Na aplicação, `classify_intent` roda depois de `mask_input`. A pesquisa treina e
  avalia cada abordagem sobre a saída de `privacy.mask_text`, a mesma função da aplicação, para não haver diferença
  entre o treino e o serviço. Isso também garante que nenhum documento, telefone, e-mail ou endereço, nem falso, vá ao LLM da abordagem 4 (nomes
  próprios são o limite conhecido do mascaramento, também na aplicação; ver design D1).
- **Regra de escolha detalhada antes dos resultados:** a elegibilidade (recall de `sensitive` ≥ 0,95 nos três testes
  somados e p95 ≤ 100 ms) e o desempate por *bootstrap* pareado da diferença de F1 macro no teste escrito por pessoas,
  em vez de "intervalos que se sobrepõem". Sem candidato elegível, nada é publicado e a decisão volta ao autor.
- **Os três testes são lidos uma única vez**, na avaliação final, com os modelos congelados. Toda escolha anterior
  (hiperparâmetros, temperatura, limiar do roteador) usa só a validação.
- **A abordagem 4 roda numa amostra:** o teste escrito por pessoas inteiro e 20 mensagens por intenção de cada teste
  sintético, por causa da cota diária do Gemini (400 chamadas). Com o cache de respostas, a execução é retomável e
  repetível sem gastar cota. Um papel novo, `intent_baseline`, na camada de LLM.
- **Grupo `finetune` separado do `research`** (torch, transformers, datasets, accelerate, onnx): o ajuste fino roda
  no Kaggle; localmente o grupo só serve para exportar o ONNX. O CI não o instala.
- **Peças comuns da pesquisa saem do antifraude:** a fixação do dataset `v1.0.0` e o envio ao Hub (`ModelHub`, tag,
  retomada) viram módulos de `research/` usados pelas duas pesquisas, sem mudar o comportamento do antifraude.
- **O formato publicado segue o vencedor, sempre sem *pickle*:** ONNX int8 e `tokenizer.json` para o BERTimbau; JSON
  com números (vocabulário, IDF e coeficientes, ou só coeficientes sobre o e5 já fixado) para as abordagens 1 e 2. A
  change 9 constrói o adaptador só do vencedor, como a change 10 fez.

## Fora de escopo

- LoRA (extra E5).
- O uso do modelo pela aplicação: porta `IntentClassifier`, adaptador, rede de segurança por palavras-chave,
  `GET /ready` e porta de F1 no CI (change 9).
- A variante com embedding maior e uma segunda família de LLM na abordagem 4 (Groq): só se sobrar tempo, como extra.
- Escrever o teste por pessoas: é tarefa do autor, fora do código, e esta change só o lê e valida.
- Rodar a abordagem 4 em todos os 990 exemplos sintéticos de teste (custaria três dias de cota).

## Pronto quando

Copiado do roadmap:

- a tabela compara F1 macro com intervalo de 95%, recall de `sensitive`, ECE, p50/p95 e custo nos três conjuntos;
- a escolha segue a regra escrita **antes** dos experimentos;
- o modelo está no Hugging Face numa revisão fixa.

Somam-se a isso: a curva de cobertura × erro com o limiar do roteador, a matriz de confusão e 20 erros comentados no
relatório, e o notebook de treino público no Kaggle.

## Capabilities

### New Capabilities

Nenhuma. É pesquisa, sem comportamento da aplicação (o roadmap diz "Spec: nenhuma"); a change declara `skip_specs`. A
spec `classificacao-intencao` nasce na change 9.

### Modified Capabilities

Nenhuma. O papel `intent_baseline` na camada de LLM é aditivo e só a pesquisa o usa.

## Impact

- Código novo: `research/intent/` (configuração, dados, as quatro abordagens, avaliação, escolha, exportação ONNX,
  publicação), `research/dataset.py` e `research/hub.py` (comuns), o notebook do Kaggle em `research/intent/notebooks/`,
  o prompt `v1` da abordagem 4.
- Código alterado: `research/fraud/` passa a usar os módulos comuns; `llm/roles.py` e `config.py` ganham o papel
  `intent_baseline` (`LLM_INTENT_BASELINE_MODEL` no `.env.example`).
- `pyproject.toml`: grupo `finetune`, marcador `finetune` fora do `pytest` padrão.
- Dados: `data/human-test/human_test.csv` entra no git quando o autor terminar de escrever.
- Docs: `docs/pesquisa-intencao.md` (relatório novo), `ml-e-avaliacao.md` 1.3, `decisoes-arquitetura.md`,
  `arquitetura.md`, `roadmap.md` (seções 7 e 9), README (números, links do modelo e do notebook).
- Ações externas, só com a autorização do autor: publicar o modelo em `G0m7s/deliverius-intent` e o notebook público
  no Kaggle.
- Depende do autor: escrever as ~150 mensagens do teste por pessoas e ter a conta do Kaggle com GPU liberada.
