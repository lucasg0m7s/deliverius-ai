## Context

Motivação em `proposal.md`, requisitos em `specs/dados-sinteticos/spec.md` e `specs/llm/spec.md`. A ideia central (o
cenário vem primeiro e o rótulo é verdade de construção), as cinco camadas, as 11 intenções, os tamanhos e as divisões
já estão em `docs/modelo-dominio.md`, seção 4, e não são rediscutidos aqui.

O estado atual que molda o desenho:

- O domínio (`orders`), o motor de política (`policy.evaluate`, puro, recebe pedido, histórico e instante), o
  mascaramento (`privacy.find_personal_data`) e a camada de LLM (`llm`, com orçamento, registro, cache em SQLite e o
  modelo falso) existem e têm spec.
- `call_or_degrade` devolve um `AIMessage`; não há chamada com saída estruturada pronta, e `langchain_core` só pode ser
  importado em `llm/`, `agent/` e `bootstrap` (regra de import 3).
- O papel `datagen` aponta para o Gemini Flash-Lite, com teto de 1.024 tokens de saída. Não há papel para o gerador do
  teste de outra família.
- O orçamento da camada de LLM vive no Postgres: gerar mensagens exige o `docker compose up`.
- **Cota conferida em 2026-11-08:** Gemini com 400 requisições por dia no padrão do projeto (80% de 500) e 2.000.000 de
  tokens; Groq com 800 requisições e 150.000 tokens por dia no padrão. O Flash-Lite ignora a temperatura.
- O Faker está só no grupo `dev`; não há `huggingface-hub`, pasta `data/` nem licença no repositório.

## Goals / Non-Goals

**Goals:**

- Rótulos que nenhum LLM decide: intenção, dado pessoal, injeção, fraude e resolução saem do código.
- Reprodutível por comando, com linhagem até o commit do gerador.
- Caber na cota gratuita de um dia por provedor, com retomada barata se não couber.
- Um formato estável, que as changes 7, 8, 10 e 15 leem sem conhecer o gerador.

**Non-Goals:**

- Realismo estatístico de um app de delivery de verdade: as taxas são plausíveis e configuráveis, não calibradas.
- Medir a qualidade dos modelos: isso é das changes 7, 8, 10 e 15.
- Carregar o mundo no Postgres (fica para a change que precisar).

## Decisions

### Estrutura

```
src/deliverius/datagen/
  __init__.py        API pública: generate_world, generate_messages, generate_rag_pairs, generate_scenarios, load_dataset
  config.py          DatagenConfig: tamanhos, taxas, instante de referência, semente (Pydantic, com padrões)
  rng.py             Geradores derivados da semente, um por camada; ids determinísticos
  world.py           Camada 1: clientes, perfis, restaurantes, pedidos, linhas do tempo, incidentes
  claims.py          Camada 2: reclamações, rótulo de fraude, ruído
  resolution.py      Camada 3: monta a proposta e chama policy.evaluate
  scenarios.py       Sorteio de cenários de mensagem (intenção, fatos, persona, marcadores pedidos)
  placeholders.py    Marcadores, preenchimento com Faker e catálogo de injeção, posições dos dados pessoais
  messages.py        Camada 4: chamadas ao LLM em lote, validação e descarte das variações
  attempts.py        Registro de tentativas: qual tentativa de cada trabalho deu a resposta guardada
  discards.py        Contagem de descartes por rótulo (motivo, intenção), com ida e volta em JSON para o registro
  rag_pairs.py       Pares pergunta → seção, com o filtro de cópia
  e2e.py             Camada 5: cenários de ponta a ponta
  splits.py          Divisão por cenário e por tempo
  records.py         Modelos Pydantic das linhas gravadas (o contrato dos arquivos)
  output.py          Gravação atômica, JSONL, manifesto, hash e leitura com conferência
  publish.py         Card e envio ao Hugging Face (com confirmação)
  human_test.py      Formato e validação do teste escrito por pessoas
  prompts/           messages/v1.md, holdout/v1.md, rag_pairs/v1.md (o layout que llm.load_prompt exige)
src/deliverius/cli/
  __main__.py        `python -m deliverius.cli datagen <subcomando>` (argparse)
```

Subcomandos: `world`, `messages`, `holdout`, `rag-pairs`, `scenarios`, `all`, `validate-human`, `publish`. Todos
recebem `--seed` e `--out`; os que chamam LLM recebem `--cache` (padrão `.cache/datagen.sqlite`).

### Rótulo por construção, LLM só com marcadores

O código sorteia o cenário de mensagem: intenção, fatos (itens, valores, minutos de atraso, status), persona (tom,
formalidade, gíria, erro de digitação, mensagem imprecisa) e quais marcadores a mensagem deve conter (`[[NOME]]`,
`[[CPF]]`, `[[TELEFONE]]`, `[[EMAIL]]`, `[[ENDERECO]]`, `[[INJECAO]]`). O LLM devolve variações com os marcadores, e o
código os troca pelo valor do Faker ou por um ataque do catálogo, gravando tipo e posição de cada dado pessoal.

- O rótulo de intenção é o do cenário, mesmo que o LLM escreva algo impreciso: mensagem imprecisa também acontece no mundo real,
  e o erro de rótulo que sobra é medido na revisão à mão.
- O catálogo de injeção (≈ 30 ataques em pt-BR: troca de papel, "ignore as regras", pedido do prompt, falso aviso do
  sistema, valor inflado) é revisado e versionado no código. O LLM nunca vê o ataque.
- Nenhum dado pessoal vai ao LLM, nem falso.

**Alternativas descartadas:**

- *LLM escreve livre e outro LLM rotula:* o rótulo vira opinião de modelo, e o classificador aprenderia o viés do
  rotulador.
- *Mandar ao LLM os dados do Faker e achar as posições depois por busca:* o LLM reformata CPF e telefone, e a busca
  erra a posição; além disso, o rótulo de privacidade passaria a depender do próprio detector que ele avalia.
- *LLM escreve o ataque:* não dá para saber se o texto é de fato um ataque, e o filtro de segurança do provedor pode
  recusar.

### Dez variações por chamada, com saída estruturada

Cada chamada pede 10 variações de um cenário, num esquema `{"variations": [str, ...]}`. São ≈ 300 cenários para ≈ 3.000
mensagens (297 cenários, 27 por intenção), ≈ 300 chamadas ao Gemini, mais 48 para os pares da política (uma por
seção, 1 ou 2 perguntas cada, 60 no total): 345 chamadas, cabe nas 400 do dia. O teto de saída do papel
`datagen` sobe de 1.024 para 2.048 tokens (10 mensagens de até 600 caracteres, com folga para o raciocínio).

As variações de um cenário ficam sempre do mesmo lado da divisão, então o lote não aumenta o vazamento.

**Alternativa descartada:** *uma mensagem por chamada:* 3.000 chamadas, oito dias de cota, e variações menos diversas
entre si (o modelo não vê as outras para variar).

### Chamada com saída estruturada na camada de LLM

`llm` ganha `call_structured(models, schema, messages, role, config) -> T | DegradedMode`, com o motivo novo
`DegradedReason.INVALID_OUTPUT`. Por dentro, `RoleModels.structured_runnable(schema)` aplica `with_structured_output`
ao principal e à reserva antes do `with_retry` e do `with_fallbacks`; o governor e o cache continuam no modelo, então
nada muda no orçamento. O `datagen` passa só texto e um modelo Pydantic, sem importar `langchain_core`.

**Alternativas descartadas:**

- *`datagen` importa `langchain_core`:* quebra a regra 3 e espalha a integração.
- *Pedir JSON em texto e validar à mão:* perde a saída estruturada nativa dos dois provedores, já conferida na change 5.

### Papel `datagen_holdout`

Papel novo, configurado por `LLM_DATAGEN_HOLDOUT_MODEL` (padrão `groq:openai/gpt-oss-120b`), com o mesmo `RoleConfig`
do `datagen`. Usa um prompt próprio (`holdout_v1.md`), escrito de outro jeito, e um fluxo aleatório separado para
sortear os cenários, com a assinatura de fatos de cada cenário conferida contra o conjunto principal. São 55 cenários
(5 por intenção), 55 chamadas e ≈ 137.000 tokens contando o raciocínio: cabe nos 150.000 do Groq, com pouca folga; o cache permite terminar no dia
seguinte.

**Alternativa descartada:** *flag na CLI com o modelo:* foge da regra "trocar o modelo de um papel é mudar uma
variável" e obrigaria a CLI a montar modelos fora da `LlmRuntime`.

### Reprodutibilidade

| Parte | Como é reproduzível |
| --- | --- |
| Mundo, reclamações, resolução, cenários, divisões | Semente: um `random.Random` por camada, derivado da semente por `hash` estável (SHA-256 de `semente:camada`), e `Faker.seed_instance`. Nunca o `random` global |
| Tempo | Instante de referência fixo na configuração (fim do período simulado), nunca o relógio real |
| IDs | UUIDv7 montado com o instante simulado e 74 bits do gerador da camada: ordenado no tempo, como na aplicação, e determinístico |
| Mensagens do LLM | O cache de respostas, com a tentativa na chave, e o **registro de tentativas** (ver abaixo). Os dois no mesmo arquivo é o que torna a camada 4 repetível; o conjunto publicado é a referência |
| Ordem | As chamadas rodam em paralelo, mas o resultado é reunido e gravado na ordem dos cenários |

**Registro de tentativas.** Uma tentativa que falha (prazo, erro do provedor, saída inválida, nenhuma variação válida) não
deixa resposta no cache, e a tentativa seguinte, com outra chave, dá certo. Sem memória disso, uma nova execução
recomeçaria na tentativa 1, chamaria o modelo de verdade e poderia receber outro texto: a saída mudaria. Por isso o
arquivo do cache ganha a tabela `datagen_attempts` (`attempts.py`), com uma linha por trabalho: a chave do trabalho e o
número da tentativa que deu certo e as contagens de descarte das tentativas anteriores. A chave é o SHA-256 do papel, do modelo, do identificador do cenário ou da seção e das
mensagens inteiras da chamada (prompt de sistema e bloco do cenário). Assim, mudar o prompt, os fatos do cenário ou o
modelo é outro trabalho e recomeça na tentativa 1. O registro guarda só hashes e números, nunca texto. Ao dar certo na
tentativa n, o trabalho grava n; numa execução seguinte ele começa direto em n. Um número maior que o máximo de tentativas
configurado é ignorado (o trabalho recomeça em 1). Trabalho que esgotou as tentativas não grava nada.

**Descartes e manifesto.** O manifesto conta os descartes de todas as tentativas, inclusive as que devolvem texto sem
nenhuma variação válida e por isso são repetidas. Para a repetição sair igual byte a byte (manifesto incluso), a linha do
registro guarda também, em JSON, as contagens de descarte das tentativas **anteriores** à vencedora, só por motivo e
intenção (ou só motivo, nos pares da política), nunca texto. Ao começar na tentativa registrada, o gerador soma essas
contagens de volta; os descartes da própria tentativa vencedora vêm de novo do cache, porque o `accept` roda sobre a
resposta guardada. Se a tentativa registrada falha numa execução futura (cache apagado, por exemplo) e a seguinte dá
certo, o registro novo leva as contagens antigas mais as da tentativa que falhou. As contagens do manifesto saem em ordem
fixa, independente da ordem em que as tarefas terminam.

Um teste de "hash dourado" gera um mundo pequeno e compara o hash com o valor fixado no teste: mudou a saída sem subir
`DATAGEN_VERSION`, o teste falha e diz para subir a versão.

**Alternativas descartadas:** *temperatura 0* (o Flash-Lite ignora a temperatura, medido na change 5); *semente do
provedor* (não é garantida por nenhum dos dois); *`uuid_utils.uuid7()`* (aleatório); *UUIDv5* (não é ordenado no
tempo).

### Formato e publicação

- **JSONL** por conjunto e divisão, mais um `manifest.json`, numa pasta `data/generated/<versão>/` fora do git. Os
  registros são modelos Pydantic em `records.py`; dinheiro em centavos e instantes em ISO 8601 UTC, como no domínio.
- **Um repositório no Hugging Face** (`deliverius-synthetic`) com uma *config* por conjunto: `messages`
  (train/validation/test), `messages_holdout` (test), `fraud_history` (train/test), `policy_qa` e `e2e_scenarios`. O card
  fica no `README.md` do repositório do dataset, com o bloco YAML de configs. A publicação cria a tag `v<versão>`.
- **Amostras** de até 50 linhas por conjunto em `data/samples/`, no git, com o `data/README.md`.
- **Licença proposta:** CC BY 4.0 para os dados. **Pendente de confirmação humana**, junto com a autorização para
  publicar.

**Alternativas descartadas:** *Parquet* (exige `pyarrow`, pesado, por ganho nenhum nesses tamanhos; o Hugging Face lê
JSONL); *CSV* (perde tipos e campos aninhados, como as posições dos dados pessoais); *um repositório por conjunto*
(cinco cards para manter).

### Antifraude: o que vai para o conjunto

Cerca de 2.000 clientes e 20.000 pedidos em seis meses. Cada reclamação traz o pedido, o instante, o que foi reclamado,
o valor pedido, a resolução da política e o rótulo `is_fraud`. O perfil do cliente e a marca de rótulo trocado ficam só
num arquivo `ground_truth` separado, para análise, fora da config de treino. A taxa de fraude entre as reclamações e a
fração de ruído vão para o manifesto e para o card. Sem texto: o antifraude usa só dados estruturados.

### Divisões

- **Mensagens:** 70/15/15 por cenário, estratificado por intenção, sorteado com a semente.
- **Antifraude:** treino nos cinco primeiros meses, teste no último; o corte vai para o manifesto. Features que olham
  o histórico (change 10) usam só o que vem antes de cada reclamação; isso é do `fraud/features.py`, não daqui.

### Cenários de ponta a ponta sem LLM

O objetivo do cliente vem de modelos de frase por fluxo, preenchidos com os fatos. O cliente simulado (change 15) já
varia a conversa a cada execução; gerar o objetivo com LLM gastaria cota sem ganho. O estado esperado sai de `evaluate`
para as ações; F5 espera transferência de prioridade alta; F4 espera só a ação que o problema real justifica; F6 espera
nenhuma ação.

**Ponto aberto para a change 15:** quando a política permite a ação, o estado final ainda depende do antifraude (que
não existe). O cenário grava a decisão da política e o rótulo de fraude, e o estado esperado supõe que o antifraude não
barra; a change 15 decide como tratar o caso em que ele barra.

### Execução sob a cota

- Concorrência limitada por um semáforo (4 por padrão); o limitador por minuto da camada de LLM faz o resto.
- Antes de começar, a CLI imprime a estimativa de chamadas por provedor e o uso já feito no dia, e recusa (saída 2)
  se o dia já está esgotado.
- **Checagem do orçamento no próprio gerador** (decidido na implementação): com cache, o governor da camada de LLM
  conta e não barra, então nunca recusaria a geração. Antes de cada tentativa, o gerador lê o uso do dia
  (`LlmUsageStore.read_usage`, só leitura) e para quando requisições ou tokens chegaram ao teto. Com concorrência 4, as
  tentativas já em voo podem passar do teto em até 3. A pausa depois de um `429` continua como segunda barreira. Uma
  resposta já guardada no cache também é barrada com o dia esgotado; no dia seguinte ela volta sem custo.
- Um `DegradedMode` por orçamento esgotado (ou sem como conferir o orçamento) interrompe a execução: as tarefas em
  andamento terminam, as pendentes não começam, a CLI informa quantos cenários faltam e sai com código de erro, sem
  gravar a saída. As respostas já obtidas estão no cache, e a próxima execução só paga o que falta.
- **Pausa curta do provedor** (decidido depois da primeira geração real): um `429` por limite por minuto (o Groq tem
  8.000 tokens por minuto) abre no `ProviderGate` uma pausa de 2 a 7 s, e as chamadas seguintes saem como
  `PROVIDER_PAUSED`. Isso não é cota esgotada. A `LlmRuntime` expõe o tempo restante da pausa do provedor do papel
  (`pause_seconds_left`) e o gerador, em `PROVIDER_PAUSED`, dorme esse tempo (`sleep` injetável) e repete **a mesma
  chamada**, sem gastar uma das 3 tentativas do cenário. A execução para como para o orçamento (saída 2, `remaining` e
  `reason=provider_paused`) se o tempo restante passa de 60 s ou se há mais de 10 pausas seguidas sem uma única
  resposta válida no meio. O contador é da execução, não do cenário: com concorrência, as esperas de vários cenários
  somam. Evento de log: `datagen.provider_pause_waited` (cenário, segundos).
- Outros motivos de modo degradado (saída inválida, erro do provedor) contam como descarte do cenário, que é tentado
  de novo com a tentativa seguinte na chave do cache, até 3 vezes.

### Dependências

Grupo novo `datagen` com `faker` (sai do `dev`) e `huggingface-hub`, listado em `[tool.uv] default-groups` junto com o
`dev`. A imagem da aplicação instala sem grupos, então não leva nenhum dos dois; a regra de import 4 garante que a
aplicação não importa `datagen`.

## Checklist de `docs/robustez.md`, seção 11

| Item | Resposta |
| --- | --- |
| Rota, ferramenta, `AccessScope` | Nenhuma rota nem ferramenta; a matriz de autorização não muda. Nenhuma consulta nova a dados de cliente |
| Corrida | Nenhuma escrita compartilhada além do orçamento, que já é atômico (change 5). Tarefas paralelas só gravam no próprio resultado em memória; a gravação dos arquivos é única, no fim |
| Idempotência | Repetir um subcomando com a mesma semente e o mesmo cache reproduz a saída. Pasta de saída já existente é recusada sem `--overwrite`. Publicar a mesma versão de novo é recusado se a tag já existir |
| Timeout e novas tentativas | As da camada de LLM (timeout por tentativa, prazo total, 2 novas tentativas). Cenário descartado: até 3 tentativas, cada uma com chave nova no cache, e a que deu certo fica no registro de tentativas. Pausa curta do provedor: espera o tempo restante e repete, sem gastar tentativa (até 60 s por espera e 10 pausas seguidas). Envio ao Hugging Face com o timeout do `huggingface-hub` e sem nova tentativa automática: o comando pode ser repetido |
| Recursos esgotáveis | Cota: orçamento da camada de LLM, estimativa prévia e parada limpa. Concorrência: semáforo. Memória: o maior conjunto (20.000 pedidos) cabe em memória com folga. Tamanho de cada variação: 600 caracteres |
| Queda no meio | Escrita atômica: tudo vai para uma pasta temporária ao lado e é renomeado no fim, com o manifesto por último. Uma queda não deixa conjunto parcial com cara de completo. O cache guarda o que já foi pago |
| Transação com rede ou LLM | Nenhuma transação nesta change; o orçamento usa as transações curtas da camada de LLM |
| Eventos de log | `datagen.run_started` (subcomando, semente, versão, estimativa), `datagen.variation_discarded` (cenário, motivo), `datagen.scenario_failed`, `datagen.provider_pause_waited` (cenário, segundos), `datagen.budget_exhausted` (faltam N), `datagen.run_finished` (contagens, descartes), `datagen.publish_dry_run`, `datagen.published`. Nenhum vira métrica: é ferramenta de desenvolvimento. Sem texto de mensagem ou prompt |
| Texto de fora e regex | A saída do LLM é validada pelo esquema e depois por regras (marcadores conhecidos, obrigatórios presentes, tamanho, repetição após `normalize_text`). A busca de marcadores é uma regex fixa e linear sobre texto limitado a 600 caracteres. O arquivo do teste humano é lido como dado e validado linha a linha, com tamanho máximo |
| Grafo, banco, API | Nenhuma mudança. `llm` ganha um papel e uma função, de forma aditiva |
| Consultas e índices | Nenhuma consulta nova |
| Travas e migrations | Nenhuma |
| Configuração nova | `LLM_DATAGEN_HOLDOUT_MODEL` (com padrão) e `HF_TOKEN` (só para publicar). A falta da chave do Groq falha na montagem do papel `datagen_holdout`, só quando ele é usado; a falta do `HF_TOKEN` falha na publicação confirmada, citando a variável. Ambas no `.env.example` |

## Checklist de `docs/seguranca.md`, seção 7

- Nenhum dado pessoal, nem falso, chega ao LLM; os valores do Faker só existem nos arquivos gerados, que são sintéticos.
- O conteúdo enviado ao Gemini gratuito pode ser usado pelo Google: aceitável, porque tudo é sintético (já registrado em
  `seguranca.md`). Os cenários de alergia e conduta são inventados.
- `HF_TOKEN` em `SecretStr`, nunca logado; teste com um valor único confere que não aparece em log nem em erro.
- O catálogo de injeção é dado de teste, nunca vai a um prompt de sistema e é gravado só no texto do cliente.
- O dataset publicado não tem dado de pessoa real; o card diz que os nomes e documentos são falsos.

## Avaliação

O gerador não treina modelo, mas usa LLM para escrever. Ele é avaliado por:

| O quê | Métrica | Limiar |
| --- | --- | --- |
| Descarte de variações | Fração descartada por intenção | ≤ 20% em cada intenção; acima disso, o prompt é revisto antes de publicar |
| Diversidade | Fração de mensagens repetidas após normalizar, por cenário e no total | 0% repetidas (são descartadas); relatório da média de palavras distintas por intenção |
| Rótulo de intenção | Revisão à mão de 100 mensagens sorteadas (estratificadas por intenção), contando "não expressa a intenção do rótulo" | ≤ 10%; o número vai para o card como limite conhecido |
| Cobertura | Contagens por intenção, divisão, fluxo e tipo de dado pessoal | Os tamanhos de `modelo-dominio.md`, seção 4, com tolerância de 10% |
| Vazamento | Testes de divisão por cenário, por tempo e de cenários disjuntos no teste de outro gerador | Zero ocorrências |
| Pares da política | Descarte pelo filtro de cópia de 5 palavras | Relatado; a qualidade da busca é medida na change 8 |

O resultado fica em `docs/dados-sinteticos.md` (relatório curto) e resumido no card.

## Risks / Trade-offs

- [O filtro de segurança do provedor recusa mensagens de assédio ou acidente na intenção `sensitive`] → O prompt
  descreve o fato em termos neutros; a taxa de descarte por intenção mostra o problema, e o limiar de 20% bloqueia a
  publicação até ajustar.
- [O LLM escreve sempre do mesmo jeito, apesar da persona] → Persona sorteada com vários eixos, 10 variações pedidas de
  uma vez e o teste de outro gerador, que mede quanto o classificador decorou o estilo.
- [Rótulo de intenção errado em mensagem imprecisa] → Medido na revisão à mão e publicado; o teste escrito por pessoas é o
  número que decide o classificador (`ml-e-avaliacao.md`).
- [Antifraude trivial demais ou impossível] → Casos difíceis e ruído de rótulo por construção; as taxas ficam na
  configuração e são ajustadas na change 10 se o PR-AUC sair perto de 1 ou do acaso, com nova versão do dataset.
- [Cota do Groq apertada para o teste de outro gerador] → Cache e retomada no dia seguinte; o lote pode cair para 8
  variações por chamada sem mudar o desenho.
- [Mudança no Faker muda a saída] → Versão fixada no `uv.lock` e o teste do hash dourado.

## Pendências humanas

- Criar a conta (ou organização) no Hugging Face e um token de escrita, posto em `HF_TOKEN` no `.env`.
- Autorizar a publicação do dataset (o comando só envia com confirmação explícita).
- Confirmar a licença CC BY 4.0 dos dados.
- Nenhuma dessas bloqueia a implementação; só o último item do critério de pronto ("o dataset está no Hugging Face").
