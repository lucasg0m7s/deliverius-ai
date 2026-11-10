# Pesquisa: quanto modelo o classificador de intenção precisa?

> **Estado:** hipótese, método e regra de escolha escritos **antes** de qualquer número de teste. As seções de
> resultados são preenchidas pela avaliação final (`python -m research.intent.evaluate`), que roda uma única vez,
> com os quatro candidatos congelados.

## Pergunta e hipótese

O nó `classify_intent` decide o caminho de cada mensagem: sensível vai para humano sem LLM, dúvida vai para a FAQ,
problema com pedido vai para o agente, e confiança baixa vira pergunta. A pergunta é **quanto modelo isso precisa**.

Hipótese: um modelo pequeno treinado nos dados sintéticos (TF-IDF ou e5 + regressão logística) chega perto do
BERTimbau ajustado no teste de clientes simulados (escrito por um terceiro autor), e a diferença não é
significativa; "só perguntar ao LLM" acerta bem,
mas não cabe no orçamento de latência (p95 ≤ 100 ms) do caminho de cada mensagem.

## Método

- **Dados:** `G0m7s/deliverius-ai` v1.0.0 (revisão fixa em `research/dataset.yaml`): treino (2.089), validação (440),
  teste do mesmo gerador (440) e teste de outro gerador (550, Groq), 11 intenções equilibradas; mais o teste escrito
  por clientes simulados (`data/human-test/human_test.csv`), validado, com o SHA-256 registrado (ver "Mudança de
  método antes dos resultados").
- **Entrada:** a saída de `privacy.mask_text`, como na aplicação. Documento, telefone, e-mail e endereço viram
  marcadores; nomes próprios não são mascarados (limite conhecido, também na aplicação).
- **Abordagens:**

  | # | Abordagem | Escolhido na validação |
  | --- | --- | --- |
  | 1 | TF-IDF de palavras (1–2) e de caracteres dentro da palavra (3–5) + regressão logística | `C` |
  | 2 | e5-small da aplicação (`embed_queries`, ONNX fixado) + regressão logística | `C` |
  | 3 | BERTimbau base com ajuste fino no Kaggle, exportado para ONNX int8 | taxa de aprendizado |
  | 4 | Gemini Flash-Lite com 2 exemplos por intenção no prompt (`intent_baseline/v1`), sem treino | nada |

- **Calibração:** *temperature scaling* nas abordagens 1 a 3, na validação; a 4 declara a própria confiança.
- **Limiar do roteador:** o menor `t` da validação com erro ≤ 5% entre as mensagens de confiança ≥ `t`.
- **Medição:** uma mensagem por vez, em CPU, com 1 thread, depois de 20 de aquecimento, do texto mascarado às
  probabilidades. A abordagem 4 roda numa amostra (o teste de clientes simulados inteiro e 20 por intenção de cada teste
  sintético), e as outras também são medidas nela, lado a lado.
- **Três testes lidos uma vez:** hiperparâmetros, temperatura e limiar usam só a validação.

## Mudança de método antes dos resultados

O plano original usava um teste **escrito por pessoas**. Ele não foi escrito, e o autor decidiu trocá-lo por um teste
de **clientes simulados**: 165 mensagens escritas por agentes do Claude (5 por intenção em cada persona), cada um seguindo uma
persona (jovem apressado com gíria e erro de digitação; pessoa mais velha, formal e prolixa; cliente frequente,
direto, sarcástico, que mistura assuntos e tenta enganar o atendente). O Claude é de uma família diferente das que
geraram o treino (Gemini) e o teste do outro gerador (Groq), então o conjunto ainda mede se o modelo generaliza para
um autor que nunca viu. Os agentes não leram os dados do projeto. Na revisão dos rótulos, uma mensagem mudou de
classe ("slk q lixo", de `quality_issue` para `other`: o texto não diz o problema), e a coluna `notes` registra isso.

A troca foi feita **antes de qualquer número de teste**: nenhum teste tinha sido lido, e a regra de escolha só mudou
de nome. O formato e o validador de `data/human-test/` continuam valendo para mensagens escritas por pessoas, que
podem entrar numa versão futura do teste.

## Regra de escolha (copiada antes dos resultados)

1. **Elegível** é a abordagem com recall de `sensitive` ≥ 0,95 nos três testes somados e p95 ≤ 100 ms em CPU.
2. Entre as elegíveis, a **melhor** é a de maior F1 macro no teste de clientes simulados.
3. Para cada outra elegível, o intervalo de 95% da diferença de F1 (melhor − outra) por *bootstrap* pareado e
   estratificado no teste de clientes simulados (2.000 reamostragens, semente fixa). Intervalo que contém zero é empate. Vence a mais
   rápida (menor p95) entre a melhor e as que empatam com ela; o tamanho em disco desempata a latência.
4. Nenhuma elegível: nada é publicado, e a decisão volta ao autor.

Consequência prevista, escrita antes: a abordagem 4 não passa no p95 (rede e LLM levam centenas de milissegundos).
Ela entra na tabela como a referência de "só perguntar ao LLM", não como candidata.

## Resultados

*A preencher pela avaliação final:* a tabela dos três testes e da amostra com F1 macro e intervalo de 95%, recall de
`sensitive` (por teste e somado), ECE, p50/p95, custo por mil mensagens e tamanho; a curva de cobertura × erro e o
limiar; a matriz de confusão do vencedor no teste de clientes simulados; 20 erros comentados; a escolha pela regra.

Já medido na validação (escolhas da etapa de treino, sem tocar os testes):

| Abordagem | F1 macro (validação) | Temperatura | Limiar do roteador |
| --- | --- | --- | --- |
| TF-IDF + logística | 0,943 | 0,313 | 0,376 |
| e5 + logística | 0,917 | 0,348 | 0,563 |
| LLM (amostra de 220 da validação) | 0,959 | — (confiança declarada) | 0,85 |

Temperatura abaixo de 1 quer dizer que a regressão logística crua era pouco confiante: a calibração afia as
probabilidades. O LLM declara confiança entre 0,85 e 1 em todas as mensagens, então o limiar cobre tudo (erro de
4,1% na amostra) e não separa as mensagens difíceis; o ECE dele na validação foi 0,026, e o recall de `sensitive`, 1,0.

**Medição de latência do LLM (corrigida na implementação):** o limitador de requisições por minuto da camada de LLM
fica dentro do modelo, e a primeira execução cronometrou também a espera na fila (p50 de 10 s). A pesquisa agora monta
o modelo sem esse limitador e mantém o mesmo ritmo esperando depois de cada chamada real, fora do trecho medido; uma
chamada real levou cerca de 1,1 s. A latência da avaliação final é medida já assim.

## Limites

- **Nenhuma pessoa escreveu o teste final.** Ele vem de um modelo de outra família, simulando três estilos de
  cliente. Mede a generalização para um terceiro autor, não para texto real; o número em produção pode ser pior.
- O teste é pequeno e tem três estilos: intervalos largos e viés de estilo.
- A validação é sintética: o limiar do roteador pode ficar frouxo em texto real.
- A latência depende da CPU usada, que o relatório informa.

## Linhagem

*A preencher:* commit, revisão do dataset, SHA-256 do teste de clientes simulados, execuções do MLflow (`deliverius-intent`) e a
revisão publicada no Hugging Face.
