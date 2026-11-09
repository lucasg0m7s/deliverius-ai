## Purpose

Dar a cada reclamação permitida pela política uma nota de fraude calibrada, com os fatores que mais pesaram e a
indicação de revisão humana pelo custo esperado do erro, a partir de um modelo treinado com as mesmas features que a
aplicação calcula, carregado numa revisão fixa e conferida.

## ADDED Requirements

### Requirement: Features no ponto do tempo
O sistema SHALL calcular as features de uma reclamação com um único código, usado tanto no treino quanto na
aplicação, e cada feature MUST considerar só fatos com instante **estritamente anterior** ao instante da reclamação.
Isso vale para pedidos, eventos de entrega e reclamações anteriores do cliente. Um fato no mesmo instante ou depois
MUST ser ignorado, mesmo que quem chama o entregue. As features SHALL ser:
- idade da conta;
- pedidos em 90 dias;
- reclamações em 30 e em 90 dias;
- valor pedido em reclamações de reembolso em 30 dias;
- razão entre reclamações e pedidos em 90 dias;
- reclamações enviadas à revisão humana em 90 dias;
- do pedido: valor, valor pedido, fração do pedido que foi pedida, tipo de ação e motivo, tempo desde a entrega, se
  a entrega estava marcada como entregue, forma de pagamento e hora local da reclamação.

#### Scenario: Fato posterior não muda nada
- **WHEN** as features de uma reclamação são calculadas uma vez com o histórico até ela e outra vez com o mesmo
  histórico mais pedidos, eventos e reclamações posteriores
- **THEN** os dois vetores de features são idênticos

#### Scenario: Fato no mesmo instante
- **WHEN** outra reclamação do mesmo cliente tem exatamente o mesmo instante da reclamação avaliada
- **THEN** ela não entra nas contagens de reclamações anteriores

#### Scenario: Entrega marcada depois da reclamação
- **WHEN** o pedido só recebe o evento "entregue" depois do instante da reclamação
- **THEN** a feature "marcada como entregue" é falsa e o "tempo desde a entrega" fica ausente

#### Scenario: Cliente sem histórico
- **WHEN** a reclamação é a primeira do cliente e ele não tem pedidos nos 90 dias anteriores
- **THEN** as contagens são zero, a razão entre reclamações e pedidos é zero e nenhuma divisão por zero acontece

### Requirement: Nenhuma feature substitui atributo sensível nem entrega o rótulo
As features MUST NOT usar endereço, CEP, bairro, cidade, nome, CPF, telefone, e-mail ou o texto da mensagem do
cliente, nem o perfil de comportamento do gerador, a marca de rótulo trocado ou o desfecho da revisão humana.
O conjunto de features SHALL ter um hash de esquema (nomes, ordem, tipos e versão) que muda sempre que uma feature
muda.

#### Scenario: Esquema fechado
- **WHEN** o teste lista as features calculadas
- **THEN** a lista é exatamente a do esquema versionado, e nenhum nome dela vem da lista de campos proibidos

#### Scenario: Esquema alterado sem versão nova
- **WHEN** uma feature é adicionada, removida ou reordenada
- **THEN** o hash do esquema muda, e um teste de hash dourado obriga a subir a versão do esquema

### Requirement: O que é uma reclamação anterior
Uma reclamação anterior SHALL ser contada da mesma forma no treino e na aplicação: uma reclamação que a política
não negou. Na aplicação, isso é um pedido de aprovação do cliente, ou uma ação executada sem aprovação. Uma ação
que nasceu de uma aprovação MUST ser contada uma vez só. O instante de uma reclamação anterior MUST ser o instante da
reclamação, para o pedido de aprovação, e o da execução, para a ação automática. A leitura MUST trazer só dados do
cliente da reclamação, na sandbox dele.

#### Scenario: Aprovação que virou ação
- **WHEN** o cliente tem um pedido de aprovação aprovado e a ação executada a partir dele
- **THEN** o histórico conta uma reclamação, e não duas

#### Scenario: Aprovação recusada
- **WHEN** o cliente tem um pedido de aprovação que o revisor recusou
- **THEN** ele conta como reclamação anterior e como reclamação enviada à revisão, sem revelar a recusa

#### Scenario: Dados de outro cliente
- **WHEN** outro cliente da mesma sandbox, ou um cliente de outra sandbox, tem pedidos e reclamações no mesmo
  período
- **THEN** nenhum deles entra nas features da reclamação avaliada

### Requirement: Nota calibrada e revisão pelo custo
O sistema SHALL devolver, para uma reclamação, a probabilidade calibrada de fraude (entre 0 e 1), a versão do modelo
e a indicação de revisão humana. A revisão MUST ser indicada quando **probabilidade × valor pedido ≥ custo de uma
revisão**, com o custo da revisão vindo do artefato publicado. Os limites da política continuam valendo por cima:
a nota nunca torna permitido o que a política mandou para aprovação.

#### Scenario: Nota média em valor pequeno
- **WHEN** a probabilidade é 0,30, o valor pedido é R$ 8,00 e o custo da revisão é R$ 4,00
- **THEN** a revisão humana não é indicada (0,30 × 8,00 = 2,40 < 4,00)

#### Scenario: Mesma nota em valor grande
- **WHEN** a probabilidade é 0,30 e o valor pedido é R$ 180,00
- **THEN** a revisão humana é indicada (54,00 ≥ 4,00)

#### Scenario: Igual ao custo
- **WHEN** probabilidade × valor pedido é exatamente o custo da revisão
- **THEN** a revisão humana é indicada

#### Scenario: Calibração igual à da pesquisa
- **WHEN** a aplicação pontua o conjunto de teste com o artefato publicado
- **THEN** cada probabilidade é igual, até 1e-9, à do modelo calibrado da pesquisa

### Requirement: Os três fatores que mais pesaram
O resultado SHALL trazer os três fatores com maior contribuição absoluta para a nota, cada um com o nome da feature
e a contribuição com sinal, ordenados do maior para o menor. Os fatores MUST NOT trazer o valor de nenhum dado
pessoal.

#### Scenario: Fatores de uma nota alta
- **WHEN** um cliente com cinco reclamações em 30 dias pede o reembolso do pedido inteiro
- **THEN** o resultado traz três fatores, ordenados pela contribuição absoluta, com os nomes das features

#### Scenario: Soma das contribuições
- **WHEN** as contribuições de todas as features e o termo de base são somados
- **THEN** a soma é igual, até 1e-6, à saída bruta do modelo antes da calibração

### Requirement: Modelo numa revisão fixa e conferida
O sistema SHALL carregar o modelo de arquivos locais baixados por um comando explícito, de um repositório do
Hugging Face numa revisão (commit) fixada no código. O manifesto MUST ter tamanho e SHA-256 fixados no código, e cada
arquivo do modelo MUST ter o tamanho e o SHA-256 listados no manifesto. O modelo MUST ser recusado, com um erro que
nomeia o arquivo ou a regra, quando um arquivo faltar ou diferir, ou quando o hash do esquema de features do
manifesto for diferente do hash do código. A aplicação MUST NOT acessar a rede para obter o modelo.

#### Scenario: Arquivo adulterado
- **WHEN** o arquivo do modelo (os coeficientes da regressão logística escolhida) foi trocado por outro do mesmo
  tamanho
- **THEN** a carga falha com "hash diferente", nomeando o arquivo, e nada é pontuado

#### Scenario: Esquema de features diferente
- **WHEN** o manifesto traz um hash de esquema diferente do calculado pelo código
- **THEN** a carga falha com "esquema de features diferente", e um evento de log registra os dois hashes

#### Scenario: Download repetido
- **WHEN** o comando de download roda com todos os arquivos já presentes e corretos
- **THEN** nada é baixado de novo

#### Scenario: Hub fora do ar
- **WHEN** o download falha por rede ou tempo esgotado
- **THEN** o comando termina com erro de serviço externo, sem deixar arquivo parcial que passe pela conferência

### Requirement: Treino reproduzível e avaliado contra baselines
A pesquisa SHALL treinar a partir do dataset numa versão conferida pelo manifesto, com divisão por tempo: treino até
agosto, validação em setembro (calibração e escolha de limiar) e teste em outubro, tocado uma vez só. A população é a
das reclamações que a política não negou. A pesquisa SHALL comparar a regra fixa "3 ou mais reclamações em 30 dias",
a regressão logística e o LightGBM calibrado em PR-AUC (com intervalo de 95% por *bootstrap*), ROC-AUC, Brier, curva
de confiabilidade, recall nos 5% mais suspeitos e custo esperado. Também SHALL comparar a regra por custo com o
limiar fixo do `TunedThresholdClassifierCV`. Cada execução MUST registrar no MLflow os parâmetros, as métricas, a
versão do dataset, o commit e a semente. A regra de escolha MUST estar escrita antes de qualquer execução e MUST
usar só a validação: o LightGBM é escolhido quando o intervalo de 95% (*bootstrap* pareado) da diferença de PR-AUC
para a regressão logística fica todo acima de zero; senão, fica o modelo mais simples. O teste MUST ser calculado uma
vez, depois da escolha.

#### Scenario: Mesma semente, mesmo modelo
- **WHEN** o treino roda duas vezes com a mesma versão do dataset e a mesma semente
- **THEN** os dois modelos dão as mesmas probabilidades no teste

#### Scenario: Dataset alterado
- **WHEN** um arquivo do dataset tem SHA-256 diferente do manifesto
- **THEN** o treino recusa o dataset antes de calcular qualquer feature

#### Scenario: LightGBM não vence o baseline
- **WHEN** o intervalo de 95% da diferença de PR-AUC entre o LightGBM e a regressão logística, na validação, inclui
  zero
- **THEN** o modelo escolhido é a regressão logística, e o relatório diz isso sem trocar o critério

### Requirement: Publicação com card e manifesto
O modelo escolhido SHALL ser publicado no Hugging Face com os arquivos do modelo, os parâmetros do calibrador, a
regra de decisão (custo da revisão), um model card (dados, métricas no teste, limites, usos não pretendidos) e um
manifesto com a versão do modelo, a versão e o commit do dataset, o ID da execução no MLflow, as métricas, o hash do
esquema de features e o SHA-256 de cada arquivo. A publicação MUST exigir confirmação explícita e MUST NOT
sobrescrever uma versão já publicada.

#### Scenario: Ensaio sem enviar
- **WHEN** a publicação roda sem a confirmação
- **THEN** ela mostra os arquivos, os hashes e o repositório de destino, e nada é enviado

#### Scenario: Versão já publicada
- **WHEN** a versão do modelo já existe como tag no repositório
- **THEN** a publicação recusa, sem enviar arquivo nenhum

### Requirement: Porta de qualidade no CI
O CI SHALL calcular a PR-AUC e o Brier do modelo publicado sobre o teste do dataset fixado, usando o código atual de
features, e MUST falhar quando a PR-AUC ficar abaixo do piso ou o Brier acima do teto. O piso e o teto vêm do valor
medido no teste, com uma margem registrada no relatório.

#### Scenario: Feature quebrada
- **WHEN** uma mudança no cálculo de uma feature faz a PR-AUC do modelo publicado cair abaixo do piso
- **THEN** o passo do CI falha e mostra a métrica medida e o piso

### Requirement: Eventos de log da pontuação
Cada pontuação SHALL gerar um evento com a versão do modelo, a probabilidade, a indicação de revisão, o tipo de ação
e a latência. A carga do modelo SHALL gerar um evento de sucesso ou de recusa, com a regra. Os eventos MUST NOT
trazer dado pessoal, o texto da mensagem nem o valor das features de histórico do cliente.

#### Scenario: Pontuação registrada
- **WHEN** uma reclamação é pontuada
- **THEN** um evento `fraud.scored` é registrado com versão, probabilidade, revisão indicada, tipo de ação e
  latência, mais o ID do pedido, e nenhum campo pessoal do cliente
