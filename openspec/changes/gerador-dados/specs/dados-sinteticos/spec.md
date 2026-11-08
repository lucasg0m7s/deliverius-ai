## Purpose

Gerar, de forma reproduzível, os dados sintéticos rotulados que treinam e avaliam o classificador de intenção, a busca
nas políticas, o antifraude e o agente, com rótulos que são verdade de construção e divisões que não vazam o teste
para o treino.

## ADDED Requirements

### Requirement: Geração reproduzível

O sistema SHALL gerar os conjuntos por um comando que recebe a semente e grava, numa pasta de saída, os arquivos e um
manifesto. Com a mesma versão do gerador, a mesma semente e o mesmo cache de respostas, a saída MUST ser idêntica byte a
byte. A geração MUST NOT ler o relógio real nem um gerador aleatório global: o tempo vem de um instante de referência
fixo e toda aleatoriedade vem de geradores derivados da semente. Os identificadores MUST ser derivados da semente.

#### Scenario: Duas execuções sem LLM

- **WHEN** o mundo, as reclamações e os cenários são gerados duas vezes com a semente 42
- **THEN** os arquivos têm o mesmo hash nas duas execuções

#### Scenario: Semente diferente

- **WHEN** a geração usa as sementes 42 e 43
- **THEN** os conjuntos são diferentes

#### Scenario: Execução repetida com o cache

- **WHEN** a geração completa é repetida com o mesmo cache de respostas
- **THEN** nenhuma chamada real de LLM é feita e os arquivos são idênticos aos da primeira execução

#### Scenario: Repetição depois de uma tentativa que falhou

- **WHEN** a primeira tentativa de um cenário falha por erro do provedor e a segunda dá certo, e a geração é repetida com o mesmo cache
- **THEN** a repetição começa esse cenário na segunda tentativa, não chama o modelo e grava arquivos idênticos byte a byte

#### Scenario: Repetição depois de uma tentativa só com descartes

- **WHEN** a primeira tentativa de um cenário devolve texto sem nenhuma variação válida, a segunda dá certo, e a geração é repetida com o mesmo cache
- **THEN** a repetição não chama o modelo e todos os arquivos, inclusive o manifesto com a contagem de descartes, são idênticos byte a byte

#### Scenario: Prompt alterado invalida o registro de tentativas

- **WHEN** o prompt de um conjunto muda depois de uma execução que registrou tentativas
- **THEN** os cenários desse conjunto recomeçam na primeira tentativa

### Requirement: Simulador do mundo

O sistema SHALL simular, sem LLM, clientes, restaurantes e seis meses de pedidos com linhas do tempo de entrega
válidas pelo ciclo de status do domínio. Cada cliente MUST ter um perfil de comportamento (honesto ≈ 90%,
oportunista ≈ 7%, fraudador ≈ 3%). Incidentes (item faltando, item errado, qualidade, atraso, falha na entrega,
cobrança duplicada) MUST ser sorteados a taxas configuradas. Os dados pessoais MUST ser falsos, em pt-BR, com CPF de
dígito válido. Todo pedido MUST respeitar as invariantes do domínio (valores em centavos, total igual a subtotal mais
taxa menos desconto, transições de status permitidas).

#### Scenario: Pedido gerado respeita o domínio

- **WHEN** o mundo é gerado
- **THEN** todo pedido pode ser construído pelos modelos do domínio sem erro de invariante, e toda linha do tempo segue
  as transições permitidas

#### Scenario: Proporção de perfis

- **WHEN** o mundo tem 2.000 clientes
- **THEN** a proporção de cada perfil fica dentro de 2 pontos percentuais da configurada

### Requirement: Reclamações com rótulo de fraude por construção

O sistema SHALL gerar reclamações a partir do perfil e dos incidentes: o honesto reclama de incidentes reais, o
fraudador inventa incidentes e o oportunista exagera (pede mais itens ou mais valor que o real). O rótulo de fraude
MUST dizer se a reclamação não tem incidente real por trás. Para o problema não ficar trivial, honestos MUST às vezes
ter vários incidentes legítimos seguidos, fraudadores MUST às vezes ter incidentes reais e uma fração configurada
(2% a 3%) dos rótulos MUST ser trocada de propósito. O perfil do cliente e a marca de rótulo trocado MUST NOT ir para o
conjunto de treino do antifraude.

#### Scenario: Fraudador inventa incidente

- **WHEN** um fraudador reclama de item faltando num pedido sem esse incidente
- **THEN** a reclamação tem rótulo de fraude verdadeiro

#### Scenario: Ruído de rótulo

- **WHEN** o histórico é gerado
- **THEN** a fração de rótulos trocados fica entre 2% e 3%, e o manifesto registra a fração

#### Scenario: Perfil fora das colunas do antifraude

- **WHEN** o conjunto do antifraude é gravado
- **THEN** ele não tem coluna de perfil nem de rótulo trocado

### Requirement: Resolução calculada pelo motor de política

O sistema SHALL calcular a resolução correta de cada reclamação com a mesma função de decisão da aplicação, a partir do
pedido, do histórico do cliente até aquele instante e do instante da reclamação. A resolução gravada MUST conter a ação
proposta, a decisão (permitida, precisa de aprovação ou negada), o motivo e o documento da política. O gerador MUST NOT
reimplementar nenhuma regra da política.

#### Scenario: Item faltando dentro do limite

- **WHEN** um cliente sem histórico reclama de um refrigerante de R$ 8,00 faltando, 20 minutos depois da entrega
- **THEN** a resolução gravada é um reembolso de R$ 8,00, permitido, com o documento `POL-03`

#### Scenario: Reclamação fora do prazo

- **WHEN** a reclamação de item faltando chega 30 horas depois da entrega
- **THEN** a resolução gravada é negada, com o motivo de prazo

### Requirement: Mensagens rotuladas pelo cenário

O sistema SHALL gerar mensagens de cliente nas 11 intenções (`item_missing`, `wrong_item`, `quality_issue`,
`late_delivery`, `not_delivered`, `billing_issue`, `cancel_order`, `coupon_question`, `policy_question`, `sensitive`,
`other`) a partir de cenários sorteados pelo código. O LLM MUST receber só os fatos do cenário e a persona e MUST
escrever só o texto; a intenção MUST vir do cenário. Dados pessoais e tentativas de prompt injection MUST entrar por
marcadores que o código preenche, com valores do Faker e de um catálogo fixo de ataques, e as posições de cada dado
pessoal MUST ser gravadas como rótulo. Nenhum dado pessoal MUST ser enviado ao LLM. Uma variação com marcador
desconhecido, marcador obrigatório ausente, texto vazio ou acima do tamanho máximo, ou repetida, MUST ser descartada e
contada.

#### Scenario: Rótulo não depende do LLM

- **WHEN** o modelo falso devolve um texto que fala de atraso para um cenário de item faltando
- **THEN** a mensagem gravada tem a intenção `item_missing`

#### Scenario: Dado pessoal por marcador

- **WHEN** o cenário pede um CPF e o LLM escreve um texto com o marcador de CPF
- **THEN** o texto gravado tem um CPF válido do Faker no lugar do marcador, e o rótulo guarda o tipo e a posição

#### Scenario: Variação inválida

- **WHEN** o LLM devolve uma variação com um marcador que não existe
- **THEN** a variação é descartada e conta no total de descartes do manifesto

#### Scenario: Nada pessoal no prompt

- **WHEN** um cenário com dados pessoais é enviado ao LLM
- **THEN** o texto enviado contém só os marcadores, nunca os valores

### Requirement: Teste sintético de outro gerador

O sistema SHALL gerar um conjunto de teste (≈ 500 mensagens) com um modelo de família diferente do gerador principal,
por um papel próprio, a partir de cenários sorteados de um fluxo aleatório separado. Nenhum cenário desse conjunto MUST
coincidir com um cenário do conjunto principal.

#### Scenario: Cenários disjuntos

- **WHEN** os dois conjuntos de mensagens são gerados
- **THEN** nenhum identificador nem combinação de fatos de cenário aparece nos dois

#### Scenario: Modelo de outra família

- **WHEN** o teste de outro gerador é gerado com a configuração padrão
- **THEN** as linhas de uso registram um provedor diferente do usado no conjunto principal

### Requirement: Divisões sem vazamento

O sistema SHALL dividir as mensagens rotuladas em treino, validação e teste **por cenário** (70/15/15), de modo que as
variações de um cenário fiquem todas do mesmo lado, e estratificar por intenção. O histórico do antifraude MUST ser
dividido **por tempo**: o teste é o último mês simulado e nenhuma reclamação do treino tem instante igual ou posterior
ao início do teste. O manifesto MUST registrar o instante de corte.

#### Scenario: Cenário de um lado só

- **WHEN** as mensagens são divididas
- **THEN** nenhum cenário tem variações em dois lados

#### Scenario: Corte por tempo

- **WHEN** o histórico é dividido
- **THEN** toda reclamação do treino é anterior ao corte e toda reclamação do teste é igual ou posterior a ele

#### Scenario: Todas as intenções em todos os lados

- **WHEN** as mensagens são divididas
- **THEN** cada uma das 11 intenções aparece no treino, na validação e no teste

### Requirement: Pares pergunta e seção da política

O sistema SHALL gerar pares (≈ 60) de pergunta de cliente e seção de política que a responde, a partir do texto das
políticas já preenchido com os números. Uma pergunta que copia uma sequência longa de palavras da seção MUST ser
descartada, para a busca por palavra-chave não vencer por construção.

#### Scenario: Pergunta que copia o trecho

- **WHEN** o LLM devolve uma pergunta com cinco palavras seguidas iguais às da seção
- **THEN** a pergunta é descartada

#### Scenario: Rótulo da seção

- **WHEN** um par é gravado
- **THEN** ele traz o documento e o título da seção de onde a pergunta foi gerada

### Requirement: Cenários de ponta a ponta

O sistema SHALL gerar cenários (≈ 150), sem LLM, com o estado inicial (cliente, pedido, itens, linha do tempo e ações
anteriores), o objetivo do cliente em português e o estado final esperado: a ação e o valor, o pedido de aprovação, a
transferência com prioridade ou nenhuma ação. Os cenários MUST cobrir os fluxos F1 a F7 de `fluxos-do-agente.md`. O
estado esperado das ações MUST vir da função de decisão da política; casos sensíveis MUST esperar transferência de
prioridade alta sem ação; tentativas de prompt injection MUST esperar nenhuma ação além da que o problema real
justifica.

#### Scenario: Reembolso alto

- **WHEN** um cenário do fluxo F2 é gerado com um pedido de R$ 186,00 marcado como entregue e não recebido
- **THEN** o estado esperado é um pedido de aprovação e nenhum reembolso executado

#### Scenario: Alergia

- **WHEN** um cenário do fluxo F5 é gerado
- **THEN** o estado esperado é uma transferência de prioridade alta e nenhuma ação

#### Scenario: Cobertura dos fluxos

- **WHEN** os cenários são gerados
- **THEN** cada fluxo de F1 a F7 tem ao menos 5 cenários

### Requirement: Manifesto e linhagem

O sistema SHALL gravar com cada geração um manifesto com a versão do gerador, a semente, o commit do código, o instante
de referência, as versões dos prompts, os modelos usados, as contagens por conjunto e por intenção, os descartes, o
instante de corte e o hash SHA-256 de cada arquivo. Um arquivo cujo hash não bate com o manifesto MUST ser recusado na
leitura.

#### Scenario: Arquivo alterado

- **WHEN** um arquivo da geração é modificado depois de gravado
- **THEN** a leitura do conjunto falha citando o arquivo

### Requirement: Publicação versionada

O sistema SHALL publicar os conjuntos num repositório do Hugging Face Datasets, com um *dataset card* que diga que os
dados são sintéticos, como foram gerados, os tamanhos, as divisões, os limites conhecidos e a licença, e marcar a
publicação com a versão do gerador. A publicação MUST exigir confirmação explícita e, sem ela, MUST só mostrar o que
seria enviado. O token MUST vir de variável de ambiente e MUST NOT aparecer em log ou mensagem de erro. Só amostras
pequenas de cada conjunto MUST ficar no repositório git.

#### Scenario: Sem confirmação

- **WHEN** o comando de publicação roda sem a confirmação
- **THEN** ele lista os arquivos e o card que seriam enviados, e nada é enviado

#### Scenario: Sem token

- **WHEN** o comando de publicação roda com a confirmação e sem o token
- **THEN** ele falha citando a variável, sem enviar nada

### Requirement: Validação do teste escrito por pessoas

O sistema SHALL definir o formato do conjunto de teste escrito por pessoas e oferecer um comando que o valida: intenção
entre as 11, texto não vazio e dentro do tamanho máximo, identificadores únicos e nenhum dado pessoal detectado pelo
mascaramento. O comando MUST listar cada linha inválida com o motivo e MUST NOT imprimir o texto de uma linha com dado
pessoal detectado.

#### Scenario: Intenção desconhecida

- **WHEN** uma linha do arquivo tem a intenção `refund_please`
- **THEN** o comando aponta a linha e o motivo, e termina com erro

#### Scenario: Dado pessoal no texto

- **WHEN** uma linha contém um CPF válido
- **THEN** o comando aponta a linha como inválida sem imprimir o texto

### Requirement: Execução sob a cota e retomada

O sistema SHALL gerar as mensagens com concorrência limitada, pela camada de LLM (orçamento, limite de requisições e
registro de uso), com o cache de respostas ligado. Com o orçamento do dia esgotado, a geração MUST parar sem gravar uma
saída parcial como se fosse completa e MUST informar quantos cenários faltam. Uma nova execução MUST reaproveitar pelo
cache tudo o que já foi gerado, e MUST recomeçar cada cenário na tentativa que deu certo antes, para não pedir de novo ao
modelo o que já foi pedido. Uma pausa curta do provedor MUST ser esperada, sem gastar tentativa do cenário. A geração
MUST mostrar, antes de começar, a estimativa de chamadas.

#### Scenario: Orçamento esgotado no meio

- **WHEN** o orçamento acaba depois de 40% dos cenários
- **THEN** a geração termina com erro, informa quantos faltam e não grava o manifesto

#### Scenario: Retomada

- **WHEN** a geração é executada de novo depois da interrupção
- **THEN** os cenários já gerados vêm do cache sem chamada real, e só os que faltam chamam o LLM

#### Scenario: Pausa curta do provedor

- **WHEN** o provedor responde `429` com uma pausa de poucos segundos e as chamadas seguintes saem como provedor pausado
- **THEN** a geração espera o tempo restante da pausa, repete a mesma chamada sem gastar uma tentativa do cenário e conclui

#### Scenario: Pausa longa ou repetida

- **WHEN** o tempo restante da pausa passa de 60 segundos, ou há mais de 10 pausas seguidas sem nenhuma resposta válida no meio
- **THEN** a geração para como no orçamento esgotado, informa quantos cenários faltam e não grava o manifesto

### Requirement: Nenhum conteúdo de mensagem em log

O sistema SHALL registrar os eventos da geração (início, cenário descartado, orçamento esgotado, fim) só com contagens,
identificadores de cenário e tipos de erro. O texto de mensagem, o texto do prompt e os dados pessoais gerados MUST NOT
aparecer em log.

#### Scenario: Descarte registrado sem texto

- **WHEN** uma variação é descartada por marcador desconhecido
- **THEN** o log traz o identificador do cenário e o motivo, sem o texto da variação
