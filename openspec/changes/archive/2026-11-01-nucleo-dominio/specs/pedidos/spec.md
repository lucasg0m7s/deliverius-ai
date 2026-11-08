## Purpose

Permite que o resto do sistema (agente, API, gerador de dados, antifraude) leia pedidos, itens e a linha do tempo
da entrega com valores exatos e sem nunca enxergar o dado de outro cliente ou de outra sandbox.

## ADDED Requirements

### Requirement: Leitura de pedido limitada pelo escopo de acesso
Toda leitura de pedido SHALL exigir um escopo de acesso, que é ou o de um cliente (sandbox e cliente) ou o de uma
sandbox inteira (para o revisor e para tarefas internas). O filtro do escopo MUST ser aplicado na própria consulta
ao banco. Um pedido fora do escopo MUST ser tratado exatamente como um pedido inexistente.

#### Scenario: Cliente lê o próprio pedido
- **WHEN** o pedido é lido com o escopo do cliente dono, na sandbox do pedido
- **THEN** o pedido é devolvido com os itens e a linha do tempo da entrega

#### Scenario: Pedido de outro cliente da mesma sandbox
- **WHEN** um pedido é lido com o escopo de outro cliente da mesma sandbox
- **THEN** o resultado é "não encontrado", igual ao de um ID que não existe

#### Scenario: Pedido de outra sandbox
- **WHEN** um pedido é lido com o escopo de um cliente ou de uma sandbox diferente da do pedido
- **THEN** o resultado é "não encontrado", igual ao de um ID que não existe

#### Scenario: Escopo da sandbox
- **WHEN** um pedido é lido com o escopo da sandbox do pedido, sem cliente
- **THEN** o pedido é devolvido, qualquer que seja o cliente dono

### Requirement: Listagem paginada dos pedidos
A listagem de pedidos SHALL devolver só os pedidos do escopo, do mais recente para o mais antigo, em páginas de no
máximo 50 itens, com um cursor opaco para a página seguinte. A paginação MUST ser estável: um pedido novo criado
entre duas páginas não faz um pedido aparecer duas vezes nem sumir.

#### Scenario: Primeira página
- **WHEN** um cliente com 60 pedidos lista os pedidos pedindo 50
- **THEN** recebe os 50 mais recentes e um cursor para a próxima página

#### Scenario: Última página
- **WHEN** o cliente pede a página seguinte com o cursor recebido
- **THEN** recebe os 10 restantes e nenhum cursor

#### Scenario: Tamanho acima do máximo
- **WHEN** a listagem é pedida com mais de 50 itens por página
- **THEN** a página tem no máximo 50 itens

#### Scenario: Cursor inválido
- **WHEN** a listagem recebe um cursor malformado ou adulterado
- **THEN** a leitura é recusada com um erro de entrada inválida, sem consultar o banco

### Requirement: Valores em centavos exatos
Todo valor em dinheiro SHALL ser representado em centavos inteiros, sem ponto flutuante. Somas e subtrações MUST
ser exatas, valores negativos MUST ser recusados, e o arredondamento de percentuais MUST seguir uma regra única:
metade para cima, em centavos.

#### Scenario: Soma exata
- **WHEN** quaisquer valores em centavos são somados
- **THEN** o resultado é exatamente a soma dos centavos, sem perda

#### Scenario: Valor negativo
- **WHEN** se tenta criar um valor negativo, ou subtrair um valor maior de um menor
- **THEN** a operação é recusada com erro

#### Scenario: Percentual com arredondamento
- **WHEN** se calcula 50% de R$ 0,05
- **THEN** o resultado é R$ 0,03

### Requirement: Integridade dos pedidos garantida pelo banco
O banco SHALL recusar dados inconsistentes mesmo que o código erre: total diferente de subtotal mais taxa menos
desconto, valor negativo, item com preço ou quantidade não positivos, status, forma de pagamento ou ator de evento
fora da lista conhecida, e pedido que aponta para um cliente de outra sandbox.

#### Scenario: Total inconsistente
- **WHEN** se grava um pedido cujo total não é subtotal mais taxa menos desconto
- **THEN** o banco recusa a gravação

#### Scenario: Cliente de outra sandbox
- **WHEN** se grava um pedido da sandbox A apontando para um cliente da sandbox B
- **THEN** o banco recusa a gravação

#### Scenario: Item com quantidade zero
- **WHEN** se grava um item com quantidade 0
- **THEN** o banco recusa a gravação

### Requirement: Ciclo de status do pedido
O pedido SHALL ter um dos status `placed`, `accepted`, `preparing`, `ready_for_pickup`, `out_for_delivery`,
`delivered`, `cancelled` ou `delivery_failed`, e a linha do tempo da entrega MUST registrar cada status alcançado,
com quem o causou e quando. Só são válidas as transições do ciclo: em frente, um passo por vez; cancelamento só antes
de sair para entrega; falha na entrega só depois de sair. As formas de pagamento SHALL ser só as do app: `pix`,
`credit_card`, `debit_card` e `meal_voucher`.

#### Scenario: Linha do tempo de um pedido entregue
- **WHEN** um pedido entregue é lido
- **THEN** a linha do tempo traz os status de `placed` a `delivered`, em ordem de instante

#### Scenario: Cancelamento depois de sair para entrega
- **WHEN** se verifica a transição de `out_for_delivery` para `cancelled`
- **THEN** ela é considerada inválida

### Requirement: Tempo sempre com fuso
Todo instante do domínio (feito em, previsão, entregue em, eventos da entrega) SHALL ser gravado e devolvido com
fuso horário, em UTC. O instante "agora" MUST vir de um relógio injetável, para que prazos possam ser testados.

#### Scenario: Instante sem fuso
- **WHEN** se tenta criar um pedido ou evento com um instante sem fuso horário
- **THEN** a criação é recusada com erro

### Requirement: Dados pessoais fora de logs e representações
Os dados pessoais do cliente (nome, CPF, telefone, e-mail, endereço) SHALL NOT aparecer na representação textual
dos objetos do domínio nem nos logs, mesmo quando um objeto ou um campo com esses nomes é registrado por engano.

#### Scenario: Cliente registrado em log
- **WHEN** um objeto de cliente, ou um campo chamado `cpf`, `email`, `phone`, `token` ou `password`, é passado a um log
- **THEN** o log mostra o valor substituído por um marcador, nunca o dado original

### Requirement: Consultas com tempo limitado
Toda consulta ao banco SHALL ter um tempo máximo de execução configurável (`DATABASE_STATEMENT_TIMEOUT_MS`, padrão
5 s) e a espera por uma conexão livre do pool MUST ter tempo máximo. Ao estourar, a consulta é interrompida pelo
banco e o chamador recebe um erro, nunca um resultado parcial.

#### Scenario: Consulta lenta
- **WHEN** uma consulta passa do tempo máximo
- **THEN** o banco a interrompe e o chamador recebe um erro de banco indisponível

#### Scenario: Pool esgotado
- **WHEN** todas as conexões do pool estão em uso por mais tempo que a espera máxima
- **THEN** o chamador recebe um erro de banco indisponível, em vez de esperar sem fim
