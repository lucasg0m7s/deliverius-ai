## Purpose

Decide, em código e de forma determinística, se uma ação proposta para um pedido (reembolso, cupom ou
cancelamento) é permitida, precisa de aprovação humana ou é negada, com o motivo, o teto e o documento da política
que a fundamenta, e manter os números da política numa fonte única que também preenche os documentos citados ao
cliente.

## ADDED Requirements

### Requirement: Decisão em três variantes, com motivo, teto e documento
A avaliação de uma ação proposta SHALL devolver exatamente uma de três decisões: **permitida**, **precisa de
aprovação** ou **negada**. Toda decisão MUST trazer o teto que a política admite para aquele caso (quando houver) e
o identificador do documento da política que a fundamenta (`POL-xx`), quando a situação for coberta. Uma decisão
negada MUST trazer um código de motivo estável; uma decisão que precisa de aprovação MUST trazer todos os códigos
de motivo que a levaram ao humano. Os códigos são identificadores em inglês, nunca texto para o cliente. Uma
proposta mal formada vinda do agente (valor zero, item que não é do pedido, quantidade inválida) MUST resultar em
decisão negada, e não em erro.

#### Scenario: Item faltando dentro do teto e dos limites
- **WHEN** o cliente com histórico limpo pede o reembolso de R$ 8,00 de um refrigerante de R$ 8,00 que faltou, num
  pedido entregue há 20 minutos
- **THEN** a decisão é permitida, com teto de R$ 8,00 e documento `POL-03`

#### Scenario: Valor acima do teto da situação
- **WHEN** o agente propõe reembolsar R$ 74,00 (o pedido inteiro) por uma bebida errada de R$ 12,00
- **THEN** a decisão é negada com o motivo "acima do teto" e teto de R$ 12,00, para o agente revisar a proposta

#### Scenario: Proposta mal formada
- **WHEN** a proposta cita um item que não pertence ao pedido, ou tem valor zero
- **THEN** a decisão é negada com o motivo correspondente, e nenhuma exceção é lançada

### Requirement: Só as combinações de ação e situação previstas são cobertas
O motor SHALL aceitar apenas estas combinações de situação e ação: item faltando → reembolso; item errado →
reembolso; qualidade → reembolso ou cupom; atraso → cupom ou cancelamento; não entregue → reembolso; cobrança
duplicada → reembolso; cancelamento pedido pelo cliente → cancelamento. Qualquer outra combinação MUST ser negada
com o motivo "não coberto", sem documento.

#### Scenario: Reembolso em dinheiro por atraso
- **WHEN** o agente propõe um reembolso (e não um cupom) por atraso
- **THEN** a decisão é negada com o motivo "não coberto"

### Requirement: Teto por situação
O teto SHALL ser calculado só a partir do pedido e dos números da fonte única:

| Situação | Teto |
| --- | --- |
| Item faltando, item errado | Preço unitário × quantidade reclamada dos itens citados |
| Qualidade | 50% do valor dos itens citados, arredondado ao centavo |
| Atraso de 30 a 60 minutos além da previsão | Cupom de 10% do total do pedido, no máximo R$ 15,00 |
| Atraso de mais de 60 minutos | Cupom de R$ 20,00, ou cancelamento com o total ainda não reembolsado |
| Não entregue, cobrança duplicada, cancelamento | Total do pedido ainda não reembolsado |

Um reembolso ou cancelamento MUST nunca levar o total reembolsado no pedido acima do total do pedido. Um valor
acima do teto MUST ser negado com o motivo "acima do teto" e o teto calculado.

#### Scenario: Qualidade com metade do item
- **WHEN** o cliente reclama de uma pizza de R$ 59,90 que chegou fria e o agente propõe R$ 29,95
- **THEN** a decisão não é negada por teto, e o teto informado é R$ 29,95

#### Scenario: Atraso de 45 minutos num pedido de R$ 186,00
- **WHEN** o pedido foi entregue 45 minutos depois da previsão e o agente propõe um cupom de R$ 18,60
- **THEN** a decisão é negada por teto, com teto de R$ 15,00

#### Scenario: Atraso abaixo de 30 minutos
- **WHEN** o pedido foi entregue 20 minutos depois da previsão e o agente propõe um cupom
- **THEN** a decisão é negada com o motivo "atraso abaixo do mínimo"

#### Scenario: Total já parcialmente reembolsado
- **WHEN** um pedido de R$ 74,00 já teve R$ 12,00 reembolsados e chega uma proposta de reembolso total por não
  entrega
- **THEN** o teto é R$ 62,00

### Requirement: Itens só podem ser reclamados uma vez
Nas situações com itens (faltando, errado, qualidade), a proposta MUST citar pelo menos um item do pedido, com
quantidade entre 1 e a quantidade ainda não reclamada daquele item. Unidades de um item que já receberam reembolso
ou cupom no pedido MUST contar como reclamadas. Nas demais situações, a proposta MUST não citar item. O atraso
MUST ser compensado no máximo uma vez por pedido: se o pedido já teve um cupom ou um cancelamento por atraso, uma
nova compensação por atraso MUST ser negada com o motivo "atraso já compensado".

#### Scenario: Mesmo item reclamado duas vezes
- **WHEN** o refrigerante de um pedido já foi reembolsado e chega uma nova proposta por esse refrigerante
- **THEN** a decisão é negada com o motivo "item já reclamado"

#### Scenario: Quantidade maior que a pedida
- **WHEN** o pedido tem 2 unidades de um item e a proposta reclama 3
- **THEN** a decisão é negada com o motivo "quantidade inválida"

#### Scenario: Segundo cupom pelo mesmo atraso
- **WHEN** o pedido já recebeu um cupom por atraso e chega outra proposta de cupom por atraso para ele
- **THEN** a decisão é negada com o motivo "atraso já compensado"

### Requirement: Estado do pedido e prazo para reclamar
O motor SHALL conferir o estado do pedido e o prazo, medidos no instante da reclamação informado por quem chama
(o motor nunca lê o relógio):

| Situação | Estado exigido | Prazo |
| --- | --- | --- |
| Item faltando, item errado, qualidade | Entregue | 24 h depois da entrega |
| Atraso | Qualquer um, exceto cancelado ou falha na entrega | Enquanto o pedido está em andamento ou até 24 h depois da entrega |
| Não entregue | Falha na entrega, ou marcado como entregue | 48 h depois do evento final (falha ou entrega) |
| Cobrança duplicada | Qualquer um | 90 dias depois de feito o pedido |
| Cancelamento | Antes de sair para entrega | — |

O atraso SHALL ser medido da previsão de entrega até a entrega, ou até o instante da reclamação se o pedido ainda
não foi entregue. Fora do estado exigido, a decisão MUST ser negada com o motivo do estado; fora do prazo, com o
motivo "prazo expirado".

#### Scenario: Item faltando reclamado depois de 24 h
- **WHEN** o cliente reclama de um item faltando 25 horas depois da entrega
- **THEN** a decisão é negada com o motivo "prazo expirado"

#### Scenario: Item faltando num pedido ainda a caminho
- **WHEN** o pedido está saindo para entrega e a proposta é de item faltando
- **THEN** a decisão é negada com o motivo "pedido não entregue"

#### Scenario: Não entregue com o pedido ainda em andamento
- **WHEN** o pedido está em preparo e a proposta é de não entregue
- **THEN** a decisão é negada com o motivo "pedido em andamento" (o caso é de atraso)

#### Scenario: Atraso medido com o pedido ainda em preparo
- **WHEN** o pedido ainda está em preparo, a previsão passou há 70 minutos e o agente propõe o cancelamento com
  reembolso total
- **THEN** a decisão não é negada, e o teto é o total ainda não reembolsado

#### Scenario: Cancelamento por atraso depois de sair para entrega
- **WHEN** o pedido saiu para entrega, está 70 minutos atrasado e o agente propõe cancelar
- **THEN** a decisão é negada com o motivo "pedido já saiu para entrega"; o cupom de R$ 20,00 continua possível

### Requirement: Casos que sempre vão para um humano
Mesmo dentro do teto e dos limites, a decisão MUST ser "precisa de aprovação" quando:

- a reclamação é de não entrega, mas o pedido está marcado como entregue;
- a reclamação é de cobrança duplicada (o sistema não tem o dado da cobrança para confirmá-la);
- o cliente pede o cancelamento depois de o restaurante aceitar o pedido e antes de sair para entrega.

#### Scenario: Não entregue, mas marcado como entregue
- **WHEN** o pedido de R$ 186,00 tem o evento "entregue" e o cliente diz que não recebeu
- **THEN** a decisão precisa de aprovação, com o motivo "marcado como entregue", entre outros

#### Scenario: Cancelamento com o pedido em preparo
- **WHEN** o cliente pede para cancelar um pedido que o restaurante já aceitou e está preparando, sem atraso
- **THEN** a decisão precisa de aprovação, com o motivo "restaurante já aceitou"

### Requirement: Limites de autoaprovação
Fora os casos isentos, a decisão MUST ser "precisa de aprovação", com cada limite estourado como motivo, quando:

- um reembolso ou cancelamento passa de R$ 50,00;
- um cupom passa de R$ 20,00;
- a soma do valor da proposta com os valores **autoaprovados** para o cliente nos últimos 30 dias passa de
  R$ 100,00. Contam reembolsos e cupons autoaprovados, de qualquer pedido; não contam os aprovados por um humano
  nem os cancelamentos antes do aceite.

O cancelamento antes de o restaurante aceitar MUST ser permitido com reembolso total, qualquer que seja o valor,
e não conta para o limite de 30 dias.

#### Scenario: Reembolso alto dentro do teto
- **WHEN** um pedido de R$ 186,00 com falha na entrega recebe a proposta de reembolso total
- **THEN** a decisão precisa de aprovação, com o motivo "acima do limite por ação"

#### Scenario: Limite de 30 dias
- **WHEN** o cliente já teve R$ 95,00 autoaprovados nos últimos 30 dias e chega uma proposta permitida de R$ 8,00
- **THEN** a decisão precisa de aprovação, com o motivo "acima do limite do cliente"

#### Scenario: Aprovação antiga sai da janela
- **WHEN** os R$ 95,00 autoaprovados foram executados há 31 dias
- **THEN** eles não contam, e a proposta de R$ 8,00 é permitida

#### Scenario: Cancelamento antes do aceite
- **WHEN** o cliente pede para cancelar um pedido de R$ 186,00 que o restaurante ainda não aceitou
- **THEN** a decisão é permitida, com teto de R$ 186,00 e documento `POL-01`

### Requirement: Reavaliação com o histórico atual
A decisão SHALL depender só das entradas: a ação proposta, o pedido, o histórico de ações do cliente e o instante
da reclamação. As mesmas entradas MUST dar sempre a mesma decisão. Uma ação já executada, incluída no histórico,
MUST passar a contar para o teto do pedido, para os itens reclamados e para o limite de 30 dias, de modo que
reavaliar uma proposta depois de outra ter saído dê a decisão correta.

#### Scenario: Segunda proposta igual depois da primeira executada
- **WHEN** um reembolso de R$ 8,00 pelo refrigerante foi permitido e executado, e a mesma proposta é avaliada de
  novo com o histórico atualizado
- **THEN** a segunda decisão é negada com o motivo "item já reclamado"

#### Scenario: Nenhum valor permitido passa do teto
- **WHEN** qualquer proposta é avaliada, com qualquer histórico
- **THEN** se a decisão não for negada, o valor proposto é menor ou igual ao teto informado

### Requirement: Fonte única dos números e documentos com marcadores
Todo número da política (tetos, percentuais, prazos e limites) SHALL existir num único lugar, usado pelo motor e
pelo preenchimento dos documentos. Os doze documentos da política (`POL-01` a `POL-12`), em português, MUST
escrever cada número por meio de um marcador com o nome do limite. O preenchimento MUST trocar cada marcador pelo
valor formatado para o cliente (reais, percentual, horas ou dias) e MUST recusar um marcador desconhecido.

#### Scenario: Documento preenchido
- **WHEN** o documento `POL-03` é preenchido
- **THEN** o texto mostra o prazo de 24 horas e o limite de autoaprovação de R$ 50,00, iguais aos que o motor usa

#### Scenario: Marcador desconhecido
- **WHEN** um documento contém um marcador que não é o nome de nenhum limite
- **THEN** o preenchimento falha e diz qual marcador e qual documento

#### Scenario: Número escrito à mão
- **WHEN** um documento escreve um valor em reais ou um prazo diretamente, sem marcador
- **THEN** o teste dos documentos falha

#### Scenario: Doze documentos
- **WHEN** os documentos da política são carregados
- **THEN** existem exatamente `POL-01` a `POL-12`, cada um com título e seções
