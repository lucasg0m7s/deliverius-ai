## Purpose

Guardar os pedidos de aprovação humana e as transferências para humano de forma que repetir a criação não duplique,
dois revisores não decidam nem assumam a mesma coisa, e a decisão registrada não possa ser reescrita, com o status
da conversa acompanhando cada passo.

## ADDED Requirements

### Requirement: Pedido de aprovação idempotente
Criar um pedido de aprovação SHALL gravar a proposta, o instante da reclamação, a decisão da política (motivos, teto
e documento) e, quando informados, a nota de fraude e os fatores, com status `pending` e versão 1. Criar de novo a
mesma proposta na mesma conversa MUST devolver o pedido existente, em qualquer status, sem gravar outro. Uma
conversa MUST ter no máximo um pedido pendente, garantido por um índice único parcial no banco; criar um pedido de
outra proposta enquanto há um pendente MUST falhar com erro de conflito.

#### Scenario: Reembolso alto vai para análise
- **WHEN** o agente pede aprovação para o reembolso de R$ 186,00 que a política mandou para humano
- **THEN** um pedido pendente é gravado com os motivos da política, a conversa passa a "aguardando aprovação" e uma
  entrada de auditoria registra o pedido

#### Scenario: Nó repetido na retomada
- **WHEN** o pedido de aprovação da mesma proposta é criado duas vezes, inclusive ao mesmo tempo
- **THEN** existe um único pedido e as duas chamadas devolvem o mesmo

#### Scenario: Segunda proposta com uma pendente
- **WHEN** a conversa já tem um pedido pendente e chega um pedido para outra proposta
- **THEN** a criação falha com conflito e o pedido pendente continua o único

### Requirement: Decisão com trava otimista e motivo
Decidir um pedido SHALL exigir o escopo da sandbox do revisor, a versão que o revisor viu, a decisão (aprovar ou
recusar) e um motivo não vazio, de no máximo 500 caracteres. A gravação MUST ser condicional ao status pendente e à
versão informada, numa única instrução. Repetir a mesma decisão sobre um pedido já decidido com ela MUST devolver
o estado atual sem gravar nada. Uma decisão diferente sobre um pedido já decidido MUST falhar com o conflito
`approval_already_decided`; uma versão velha num pedido ainda pendente MUST falhar com o conflito
`approval_version_stale`. A decisão MUST mudar a conversa de volta para "aberta" e gerar uma entrada de auditoria
com o revisor, na mesma transação; o texto do motivo MUST NOT ir para os dados da auditoria.

#### Scenario: Revisor aprova
- **WHEN** o revisor aprova o pedido pendente na versão 1, com motivo
- **THEN** o pedido fica aprovado, na versão 2, com quem decidiu, quando e o motivo

#### Scenario: Dois revisores decidem juntos
- **WHEN** dois revisores decidem o mesmo pedido ao mesmo tempo, um aprovando e outro recusando
- **THEN** só uma decisão é gravada e a outra falha com `approval_already_decided`

#### Scenario: Mesma decisão repetida
- **WHEN** o mesmo revisor reenvia a aprovação de um pedido que já aprovou
- **THEN** recebe o pedido aprovado, e nenhuma nova auditoria é gravada

#### Scenario: Decisão sem motivo
- **WHEN** a decisão chega com o motivo vazio ou só com espaços
- **THEN** a decisão é recusada como entrada inválida e o pedido continua pendente

#### Scenario: Versão velha
- **WHEN** o revisor decide com a versão 1 um pedido pendente que já está na versão 2
- **THEN** a decisão falha com `approval_version_stale`

#### Scenario: Pedido de outra sandbox
- **WHEN** um revisor decide um pedido de outra sandbox
- **THEN** a decisão falha com "não encontrado"

### Requirement: Decisão imutável no banco
O banco SHALL recusar qualquer alteração de um pedido de aprovação que já saiu de `pending`, mesmo que o código
tente, para que quem lê uma aprovação decidida possa contar com ela até a execução.

#### Scenario: Tentativa de reescrever uma decisão
- **WHEN** um comando tenta mudar o status ou o motivo de um pedido já aprovado
- **THEN** o banco recusa o comando e o pedido continua como estava

### Requirement: Fila e leitura no escopo
A fila de pedidos pendentes SHALL listar só os da sandbox do revisor, do mais antigo para o mais novo, em páginas
de no máximo 50 itens com cursor opaco, sem `OFFSET`. Ler um pedido MUST exigir um escopo de acesso: o cliente
dono lê o pedido da própria conversa, o revisor lê os da sandbox, e o de outro escopo tem o mesmo resultado de
pedido inexistente.

#### Scenario: Fila paginada
- **WHEN** a sandbox tem 60 pedidos pendentes e o revisor pede a fila
- **THEN** recebe os 50 mais antigos e um cursor, e a página seguinte traz os 10 restantes, sem repetir nem pular

#### Scenario: Cliente lê pedido de outro cliente
- **WHEN** um cliente lê o pedido de aprovação da conversa de outro cliente
- **THEN** o resultado é "não encontrado"

### Requirement: Transferência para humano com resumo estruturado
Criar uma transferência SHALL gravar a prioridade (normal ou alta) e um resumo estruturado (intenção, fatos e ações
já feitas, cada um com tamanho limitado), com status `open`, e mudar a conversa para "transferida", com auditoria,
na mesma transação. Uma conversa MUST ter no máximo uma transferência aberta; criar de novo MUST devolver a
existente. O resumo MUST ser recusado se contiver dado pessoal detectável, porque o texto do resumo vai para o
revisor e é guardado.

#### Scenario: Alergia transferida
- **WHEN** o fluxo sensível transfere uma conversa com prioridade alta e o resumo "cliente relata mal-estar"
- **THEN** a transferência aberta é gravada com o resumo e a conversa fica "transferida"

#### Scenario: Resumo com CPF
- **WHEN** um fato do resumo contém um CPF
- **THEN** a criação é recusada como entrada inválida e nada é gravado

### Requirement: Assumir uma transferência
A fila de transferências abertas SHALL listar só as da sandbox do revisor, prioridade alta primeiro e, dentro da
prioridade, a mais antiga primeiro, paginada como a fila de aprovações. Assumir uma transferência MUST gravar o
revisor numa instrução condicional ao status aberto; dois revisores assumindo juntos MUST resultar em um só, e o
outro MUST receber o conflito `handoff_already_assigned`. O mesmo revisor assumindo de novo MUST receber o estado
atual.

#### Scenario: Dois revisores assumem juntos
- **WHEN** dois revisores assumem a mesma transferência ao mesmo tempo
- **THEN** só um fica como responsável e o outro recebe `handoff_already_assigned`

### Requirement: Rastro de aprovações e transferências
Criar, repetir, decidir e assumir SHALL emitir eventos de log estruturados, sem dado pessoal nem o texto do motivo,
incluindo os conflitos de decisão e de versão, que viram métrica do painel.

#### Scenario: Conflito registrado
- **WHEN** uma decisão falha com `approval_already_decided`
- **THEN** um evento de log registra o conflito com o ID do pedido e a versão
