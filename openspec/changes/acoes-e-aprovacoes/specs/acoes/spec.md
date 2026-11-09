## Purpose

Executar reembolso, cupom e cancelamento de forma idempotente e auditada, com a política reavaliada dentro de uma
trava do cliente e do pedido no instante da execução, para que repetir, retomar ou disparar operações juntas nunca
gere uma ação duplicada nem passe de um teto ou limite da política.

## ADDED Requirements

### Requirement: Execução só com a política reavaliada na hora
A execução de uma ação SHALL reavaliar a proposta no motor de política dentro da mesma transação que grava a ação,
com o pedido e o histórico do cliente lidos depois de travados. A ação MUST ser gravada apenas quando a reavaliação
for **permitida**, ou quando ela **precisar de aprovação** e quem chama apresentar uma aprovação humana aprovada
para a mesma proposta. Uma reavaliação **negada** MUST NOT gravar nada, mesmo com aprovação humana: a decisão
humana autoriza a proposta, não dispensa a política. O resultado devolvido MUST dizer se a ação foi executada ou
trazer a decisão da política que impediu a execução.

#### Scenario: Reembolso automático permitido
- **WHEN** a execução recebe o reembolso de R$ 8,00 de um item faltando, num pedido entregue há 20 minutos, de um
  cliente com histórico limpo, sem aprovação humana
- **THEN** um reembolso de R$ 8,00 ligado ao item é gravado como aprovado automaticamente, com uma entrada de
  auditoria, e o resultado diz "executada"

#### Scenario: Precisa de aprovação e não há aprovação
- **WHEN** a execução recebe um reembolso de R$ 186,00 sem aprovação humana
- **THEN** nada é gravado e o resultado traz a decisão "precisa de aprovação" com os motivos

#### Scenario: Aprovado pelo revisor
- **WHEN** a execução recebe o mesmo reembolso de R$ 186,00 com uma aprovação humana aprovada para essa proposta
- **THEN** o reembolso é gravado como aprovado pelo revisor, apontando a aprovação

#### Scenario: Aprovação de outra proposta
- **WHEN** a execução recebe uma aprovação aprovada que pertence a outra conversa ou a outra proposta
- **THEN** nada é gravado e a execução falha com erro de conflito

#### Scenario: Política nega depois da aprovação
- **WHEN** uma aprovação foi aprovada, mas antes da execução outro reembolso no mesmo pedido consumiu o valor que
  restava
- **THEN** nada é gravado, o resultado traz a negativa "acima do teto" e um evento de log registra a negativa na
  reavaliação

### Requirement: Prazo contado a partir da reclamação
A reavaliação SHALL usar como instante da reclamação o momento em que o cliente reclamou, informado por quem chama
e guardado na aprovação, e não o instante da execução. Uma aprovação decidida horas depois MUST NOT perder o prazo
por causa da espera; o pedido e o histórico, ao contrário, MUST ser os atuais.

#### Scenario: Decisão do revisor depois do prazo
- **WHEN** o cliente reclamou de um item faltando 23 horas depois da entrega e o revisor aprovou 2 horas depois
- **THEN** a reavaliação considera a reclamação dentro das 24 horas e a ação pode ser executada

### Requirement: Idempotência pela conversa e pela proposta
Toda ação SHALL ter uma chave de idempotência derivada da conversa e da proposta (pedido, tipo, motivo, valor e
itens, sem depender da ordem dos itens). A chave MUST ser única no banco. Executar de novo a mesma proposta na
mesma conversa MUST devolver a ação já gravada, marcada como repetição, sem gravar outra ação nem outra entrada de
auditoria, mesmo que as duas execuções aconteçam ao mesmo tempo.

#### Scenario: Retomada duas vezes
- **WHEN** a mesma execução aprovada roda duas vezes, uma depois da outra
- **THEN** existe um único reembolso, e a segunda chamada devolve esse reembolso como repetição

#### Scenario: Mesma ação disparada junta
- **WHEN** duas execuções da mesma proposta na mesma conversa são disparadas ao mesmo tempo
- **THEN** existe um único reembolso e uma única entrada de auditoria

#### Scenario: Itens em outra ordem
- **WHEN** a mesma proposta chega com os itens listados em outra ordem
- **THEN** a chave é a mesma e a ação não é duplicada

### Requirement: Teto e limites respeitados sob concorrência
Execuções concorrentes SHALL ser serializadas por cliente e por pedido: a execução MUST travar o cliente e depois o
pedido, nessa ordem, antes de ler o histórico. Assim, a soma das ações de um pedido MUST NOT passar do total do
pedido e a soma das ações autoaprovadas de um cliente em 30 dias MUST NOT passar do limite, mesmo com execuções
disparadas juntas. Se a trava não for obtida dentro do tempo máximo de espera, a execução MUST falhar com erro de
conflito sem gravar nada, e pode ser repetida porque é idempotente.

#### Scenario: Dois reembolsos diferentes no mesmo pedido
- **WHEN** duas conversas executam ao mesmo tempo reembolsos diferentes cuja soma passa do total ainda não
  reembolsado do pedido
- **THEN** só um é gravado e o outro recebe a negativa "acima do teto"

#### Scenario: Dois reembolsos automáticos em pedidos diferentes
- **WHEN** duas execuções automáticas ao mesmo tempo, em pedidos diferentes do mesmo cliente, juntas passariam do
  limite autoaprovado de 30 dias
- **THEN** só uma é gravada e a outra recebe "precisa de aprovação" pelo limite do cliente

#### Scenario: Sequência qualquer de propostas
- **WHEN** uma sequência qualquer de propostas válidas e inválidas é executada sobre o mesmo pedido
- **THEN** a soma do que foi gravado nunca passa do total do pedido

### Requirement: Cancelamento altera o pedido na mesma transação
Um cancelamento executado SHALL mudar o status do pedido para `cancelled` e acrescentar o evento `cancelled` à
linha do tempo, com o ator `system`, na mesma transação da ação e da auditoria. A transição MUST respeitar as
transições permitidas do pedido; se o pedido já tiver saído para entrega quando a trava for obtida, a reavaliação
nega e nada muda.

#### Scenario: Cancelamento antes do aceite
- **WHEN** o cliente cancela um pedido ainda em `placed`
- **THEN** o pedido fica `cancelled`, a linha do tempo ganha o evento, e a ação de cancelamento é gravada com o
  total como valor

#### Scenario: Pedido saiu para entrega durante a espera
- **WHEN** um cancelamento aprovado pelo revisor é executado depois de o pedido passar para `out_for_delivery`
- **THEN** nada é gravado e o resultado traz a negativa da política

### Requirement: Escopo do cliente em toda execução
A execução SHALL receber o escopo de acesso do cliente da conversa, e o pedido e o cliente MUST ser buscados e
travados com esse escopo no filtro da consulta. Pedido de outro cliente ou de outra sandbox MUST ter o mesmo
resultado de pedido inexistente. A conversa MUST pertencer ao mesmo cliente e ao mesmo pedido.

#### Scenario: Pedido de outro cliente
- **WHEN** a execução recebe o ID de um pedido que pertence a outro cliente da mesma sandbox
- **THEN** a execução falha com "não encontrado" e nada é gravado

### Requirement: Ações executadas legíveis no escopo
As ações executadas SHALL poder ser lidas por pedido dentro de um escopo de acesso (o do cliente dono ou o da
sandbox), com o filtro do escopo na consulta. Ação de outro escopo MUST ser invisível.

#### Scenario: Cliente lê as próprias ações
- **WHEN** o cliente dono lê as ações do pedido
- **THEN** recebe as ações executadas no pedido, e outro cliente não recebe nenhuma

### Requirement: Rastro de cada execução
Cada execução SHALL emitir um evento de log estruturado com o resultado (executada, repetida, negada na
reavaliação, precisa de aprovação, conflito de trava), sem dado pessoal. Cada ação gravada MUST ter uma entrada de
auditoria na mesma transação, com o tipo, o valor, quem aprovou e a aprovação, quando houver.

#### Scenario: Auditoria desfeita com a ação
- **WHEN** a transação da execução é desfeita no meio (cancelamento da tarefa ou erro do banco)
- **THEN** não fica nem a ação, nem a auditoria, nem a mudança no pedido
