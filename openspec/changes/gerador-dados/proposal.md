## Why

Change 6 do `docs/roadmap.md` (`gerador-dados`, fase F1, marco M1 "dados prontos"), que cobre os requisitos **R05**
(pipelines de IA reproduzíveis) e **R07** (modelos e datasets publicados). O classificador de intenção (change 7), a
busca nas políticas (change 8), o antifraude (change 10) e os evals (change 15) precisam de dados rotulados que não
existem: sem um gerador reproduzível, cada um inventaria os seus, os rótulos seriam opinião de um LLM e não haveria
como provar que o teste não vazou para o treino.

## What Changes

Escopo copiado do roadmap: `datagen/` e a CLI, nas cinco camadas de `modelo-dominio.md`, seção 4.

- **Simulador do mundo** com semente: clientes com perfil de comportamento (honesto, oportunista, fraudador),
  restaurantes, seis meses de pedidos e linhas do tempo, incidentes a taxas realistas, dados pessoais falsos com o
  Faker pt-BR.
- **Reclamações e rótulos de fraude:** o rótulo é verdade de construção (a reclamação tem ou não um incidente real),
  com casos difíceis e 2% a 3% de rótulos trocados de propósito.
- **Resolução correta pelo motor de política:** a mesma função `evaluate` da aplicação calcula a decisão e o teto.
- **Mensagens escritas pelo LLM** (papel `datagen`): o LLM só escreve o texto; intenção, dados pessoais e tentativa de
  prompt injection são decididos pelo código e entram por marcadores, então o rótulo nunca depende do LLM.
- **Cenários de ponta a ponta** com estado inicial, objetivo do cliente e estado final esperado (estilo τ-bench).
- **Teste sintético de outro gerador** (Groq, outra família de modelo) e **publicação no Hugging Face Datasets**, com
  versão e *dataset card*.

Detalhado a partir do escopo (o roadmap e `modelo-dominio.md` são atualizados junto):

- **Mensagens divididas em treino, validação e teste por cenário** (70/15/15). `modelo-dominio.md` fala em "treino e
  validação", mas `ml-e-avaliacao.md` exige o teste sintético do mesmo gerador ao lado dos outros dois.
- **Pares pergunta → seção da política** (≈ 60) gerados pelo LLM a partir do texto já preenchido, com um filtro que
  recusa pergunta que copia o trecho (senão a busca por palavra-chave venceria por construção).
- **Formato e validação do teste escrito por pessoas:** o modelo do arquivo e um comando que confere rótulos, tamanho
  e ausência de dado pessoal real. Escrever as mensagens continua sendo tarefa humana.
- **Manifesto de cada versão:** versão do gerador, semente, commit, contagens e hash de cada arquivo, para a linhagem
  de `ml-e-avaliacao.md` (modelo → execução → versão do dataset → commit do gerador).
- **Na camada de LLM:** um papel novo, `datagen_holdout`, para o gerador do teste de outra família; e uma chamada com
  saída estruturada que devolve o objeto validado ou o modo degradado, para `langchain_core` continuar só em `llm/`.

## Fora de escopo

Do roadmap:

- O teste escrito por pessoas (≈ 150 mensagens), que é tarefa humana. Aqui entram só o formato e a validação.

Decorrentes:

- Carregar o mundo gerado no Postgres (seed da demo e das sandboxes): entra na change que primeiro precisar dele.
- As features do antifraude (`fraud/features.py`, change 10) e a indexação das políticas (change 8). O gerador
  entrega dados brutos e rótulos.
- O harness que roda os cenários de ponta a ponta (change 15).
- Upload para o Kaggle (extra da F6).
- Juiz de LLM para conferir se a mensagem expressa a intenção: a conferência aqui é amostral, à mão.
- Nenhuma rota, tabela ou migration nova.

## Critério de pronto

Copiado do roadmap:

- A mesma versão e a mesma semente geram os mesmos dados (teste).
- Existem os conjuntos e tamanhos de `modelo-dominio.md`, seção 4.
- A divisão por cenário e a divisão por tempo têm teste contra vazamento.
- O dataset está no Hugging Face com card.

Acrescentado por esta change:

- A camada de LLM é reproduzível pelo cache: rodar de novo com o mesmo cache não faz nenhuma chamada real e produz
  arquivos idênticos; uma execução interrompida retoma sem pagar de novo o que já foi gerado.
- Nenhum rótulo vem do LLM: um teste com o modelo falso prova que intenção, dados pessoais e injeção vêm do cenário.
- Os cenários de ponta a ponta cobrem os fluxos F1 a F7, e o estado esperado de cada um bate com `evaluate`.
- Uma amostra de 100 mensagens revisada à mão, com os defeitos contados no relatório.

## Capabilities

### New Capabilities

- `dados-sinteticos`: simulador do mundo, rótulos de fraude, resolução pela política, mensagens rotuladas, teste de
  outro gerador, pares de busca, cenários de ponta a ponta, divisões sem vazamento, manifesto, publicação e validação
  do teste humano.

### Modified Capabilities

- `llm`: papel novo `datagen_holdout`; chamada com saída estruturada validada, com saída inválida como modo degradado.

## Impact

- **Código novo:** `src/deliverius/datagen/` (com os prompts), `src/deliverius/cli/`, `tests/unit/datagen/`,
  `tests/unit/cli/`, `tests/integration/test_datagen_*.py`, `tests/llm/test_datagen_*.py`.
- **Código alterado:** `llm/` (papel e chamada estruturada), `config.py` (variável do papel novo).
- **Dados:** `data/` com amostras pequenas, README e o modelo do teste humano. Os conjuntos completos ficam fora do git
  (no Hugging Face). O cache da geração fica em `.cache/`, no `.gitignore`.
- **Dependências:** grupo novo `datagen` (`faker`, `huggingface-hub`), instalado por padrão no desenvolvimento e fora
  da imagem da aplicação.
- **Qualidade:** contratos do import-linter (`datagen` e `cli` como entradas, nenhum módulo da aplicação os importa).
- **Documentação:** `modelo-dominio.md`, `arquitetura.md`, `ml-e-avaliacao.md`, `seguranca.md`,
  `decisoes-arquitetura.md`, `glossario.md`, `roadmap.md`, `README.md`, `data/README.md` e o *dataset card*.
- **Cota:** uma geração completa gasta cerca de 345 chamadas ao Gemini (cabe nas 400 do dia) e 55 ao Groq; o cache
  permite dividir em dias.
- **Pendências humanas:** conta no Hugging Face e token de escrita no `.env`; autorização para publicar; confirmar a
  licença CC BY 4.0 dos dados.
- **API, banco e grafo:** nenhuma mudança.
