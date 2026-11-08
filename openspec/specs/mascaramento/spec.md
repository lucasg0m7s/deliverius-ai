# mascaramento Specification

## Purpose

Garantir que dado pessoal escrito em texto livre seja trocado por um marcador antes de chegar ao LLM, ao banco ou ao
log, de forma irreversível, com o limite do reconhecimento medido e escrito.

## Requirements

### Requirement: Normalização do texto antes do reconhecimento

O sistema SHALL normalizar todo texto antes de reconhecer dado pessoal: remover caracteres de formatação invisíveis,
aplicar NFKC, converter dígitos de qualquer alfabeto para `0-9`, converter variações de hífen para `-` e reduzir
espaços horizontais repetidos a um só. A normalização MUST ser idempotente, e o texto que sai do mascaramento é o
texto normalizado.

#### Scenario: Dígitos de largura total

- **WHEN** o texto contém `１２３.４５６.７８９-０９`
- **THEN** o texto normalizado contém `123.456.789-09`, e o mascaramento esconde esse valor

#### Scenario: Caracteres invisíveis entre os dígitos

- **WHEN** o texto contém um CPF com espaço de largura zero ou hífen condicional entre os dígitos
- **THEN** o valor é reconhecido e não aparece na saída

#### Scenario: Dígitos de outro alfabeto e travessão

- **WHEN** o texto contém um CPF com dígitos árabe-índicos ou com travessão no lugar do hífen
- **THEN** o valor é reconhecido e não aparece na saída

#### Scenario: Normalizar de novo não muda nada

- **WHEN** um texto qualquer é normalizado duas vezes
- **THEN** o segundo resultado é igual ao primeiro

### Requirement: Reconhecimento de documentos

O sistema SHALL reconhecer CPF e CNPJ quando o dígito verificador é válido. O CNPJ MUST aceitar o formato numérico e
o alfanumérico, em maiúsculas ou minúsculas. Quando o valor está na pontuação completa do documento, o sistema MUST
reconhecê-lo mesmo com o dígito verificador inválido, porque um documento digitado errado continua sendo quase um
dado pessoal.

#### Scenario: CPF com e sem pontuação

- **WHEN** o texto contém um CPF válido, como `529.982.247-25` ou `52998224725`
- **THEN** os dois são reconhecidos como CPF

#### Scenario: Onze dígitos soltos sem dígito verificador válido

- **WHEN** o texto contém 11 dígitos sem pontuação, com dígito verificador de CPF inválido, que também não formam
  um telefone
- **THEN** o valor não é reconhecido como CPF

#### Scenario: CPF pontuado com dígito errado

- **WHEN** o texto contém `529.982.247-26`
- **THEN** o valor é reconhecido como CPF

#### Scenario: CNPJ alfanumérico oficial

- **WHEN** o texto contém `12.ABC.345/01DE-35`, ou `12ABC34501DE35`, ou `12abc34501de35`
- **THEN** o valor é reconhecido como CNPJ

#### Scenario: Palavra comum que parece CNPJ sem pontuação

- **WHEN** o texto contém uma sequência de 14 letras e dígitos sem pontuação, com dígito verificador inválido
- **THEN** a sequência não é reconhecida como CNPJ

### Requirement: Reconhecimento de cartão, contato e endereço

O sistema SHALL reconhecer:

- cartão de 12 a 19 dígitos, contínuos ou em grupos, com Luhn válido;
- telefone brasileiro, com ou sem DDD e `+55`, incluindo celular com o nono dígito e números `0800`, `0300`, `0500` e
  `0900`;
- e-mail;
- CEP, com ou sem hífen;
- endereço, quando um logradouro comum é seguido de nome e número.

Oito dígitos sem separador MUST contar como CEP. O número reconhecido MUST NOT ser parte de uma sequência maior de
letras e dígitos.

#### Scenario: Cartão com e sem Luhn válido

- **WHEN** o texto contém `4111 1111 1111 1111` e, em outro ponto, `4111 1111 1111 1112`
- **THEN** só o primeiro é reconhecido como cartão

#### Scenario: Telefone com código do país não vira cartão

- **WHEN** o texto contém `+55 11 99570-1543`
- **THEN** o valor inteiro, com o `+55`, é reconhecido como telefone

#### Scenario: E-mail com dígitos não é quebrado

- **WHEN** o texto contém `11987654321@exemplo.com`
- **THEN** o valor inteiro é reconhecido como e-mail

#### Scenario: Endereço com logradouro comum

- **WHEN** o texto contém `moro na Av. Paulista, nº 1000` ou `Rua das Flores 25`
- **THEN** o logradouro, o nome e o número são reconhecidos como endereço

#### Scenario: Palavra de logradouro raro em frase comum

- **WHEN** o texto contém `comprei na via 2 vezes`, `o vale 50 reais` ou `o conjunto 2`
- **THEN** nada é reconhecido como endereço

### Requirement: Texto comum atravessa sem mudança

O sistema MUST NOT reconhecer como dado pessoal preços, horários, datas por extenso, quantidades, versões,
identificadores UUID ou carimbos de tempo ISO 8601.

#### Scenario: Mensagem comum de pedido

- **WHEN** o texto é `paguei R$ 1.234,56 e chegou às 19h30 de 02/11/2026`
- **THEN** a saída é igual ao texto normalizado

#### Scenario: Identificadores usados nos logs

- **WHEN** o texto contém `0193a2b4-7c1e-7f3a-9d2b-123456789012` e `2026-11-02T03:46:02.123456Z`
- **THEN** os dois continuam intactos

### Requirement: Marcadores tipados e estáveis na conversa

No mascaramento de uma conversa, o sistema SHALL trocar cada valor por um marcador com o tipo e um número (`[CPF_1]`,
`[CNPJ_1]`, `[CARTAO_1]`, `[TELEFONE_1]`, `[EMAIL_1]`, `[CEP_1]`, `[ENDERECO_1]`). O mesmo valor MUST receber o mesmo
marcador em toda a conversa, mesmo escrito de outra forma, e valores diferentes MUST receber números diferentes. O
resultado SHALL informar quantos valores de cada tipo foram trocados, sem o valor.

#### Scenario: O mesmo valor em turnos diferentes

- **WHEN** o cliente envia `529.982.247-25` num turno e `52998224725` no seguinte
- **THEN** os dois viram `[CPF_1]`

#### Scenario: Dois valores do mesmo tipo

- **WHEN** a mensagem contém dois telefones diferentes
- **THEN** o primeiro vira `[TELEFONE_1]` e o segundo `[TELEFONE_2]`

#### Scenario: Conversas diferentes

- **WHEN** o mesmo CPF aparece em duas conversas
- **THEN** o registro de uma não permite relacioná-lo ao registro da outra

### Requirement: O mascaramento é irreversível

O registro de marcadores de uma conversa MUST NOT guardar o valor original nem um hash dele que possa ser recalculado
sem um segredo. A impressão digital SHALL depender de uma chave secreta com pelo menos 32 bytes e do identificador
da conversa. Uma chave menor MUST ser recusada.

#### Scenario: Registro serializado

- **WHEN** o registro de uma conversa que recebeu um CPF é convertido em texto
- **THEN** nem o CPF nem seus dígitos aparecem nele

#### Scenario: Chave curta

- **WHEN** o mascaramento recebe uma chave com menos de 32 bytes
- **THEN** a operação é recusada com um erro de código estável

### Requirement: Limites sem vazamento

Quando o registro atingir 100 valores, o sistema SHALL continuar mascarando com o marcador sem número (`[CPF]`), sem
guardar o valor. Um texto com mais de 64.000 caracteres, antes ou depois da normalização, MUST ser trocado por inteiro
por `[TEXTO_OMITIDO]`. Em nenhum desses casos o texto original aparece na saída.

#### Scenario: Registro cheio

- **WHEN** o registro já tem 100 valores e chega um CPF novo
- **THEN** o CPF vira `[CPF]`, e o registro continua com 100 valores

#### Scenario: Texto grande demais

- **WHEN** o texto tem 64.001 caracteres
- **THEN** a saída é `[TEXTO_OMITIDO]`

#### Scenario: Texto que cresce ao normalizar

- **WHEN** o texto cabe no limite, mas passa dele depois do NFKC
- **THEN** a saída é `[TEXTO_OMITIDO]`

### Requirement: Mascarar de novo não muda o resultado

Aplicar o mascaramento sobre um texto já mascarado MUST devolver o mesmo texto, para que as barreiras seguintes
(logger, exportador de traces) possam agir sobre o que já passou pela primeira.

#### Scenario: Segunda barreira sobre texto mascarado

- **WHEN** o filtro do logger recebe `meu CPF é [CPF_1] e o telefone [TELEFONE_1]`
- **THEN** a saída é igual à entrada

### Requirement: Tempo limitado com entrada hostil

Nenhum reconhecimento SHALL levar mais de 50 ms sobre uma entrada de 1.000 caracteres feita para travar expressões
regulares.

#### Scenario: Entradas repetitivas

- **WHEN** o texto é uma repetição longa de dígitos, separadores, `@`, nomes de logradouro ou caracteres que crescem
  no NFKC
- **THEN** o mascaramento termina em menos de 50 ms

### Requirement: Filtro de texto livre nos logs

O sistema SHALL aplicar o mascaramento, sem numeração, a todo texto de um evento de log: a mensagem, os campos, os
valores aninhados, a representação de objetos e o texto de exceções, em todos os formatos de saída e para logs da
biblioteca padrão. Números inteiros MUST atravessar sem mudança. Se a estrutura passar de 8 níveis, o valor MUST ser
trocado pelo marcador de redação, em vez de falhar.

#### Scenario: Mensagem e campo com dado pessoal

- **WHEN** um log tem `529.982.247-25` na mensagem e num campo de nome comum, como `note`
- **THEN** o log sai com `[CPF]` nos dois lugares

#### Scenario: Exceção com dado pessoal

- **WHEN** uma exceção cuja mensagem contém um CPF é registrada, por structlog ou pela biblioteca padrão, em JSON ou
  no console
- **THEN** o CPF não aparece na saída

#### Scenario: Objeto com dado pessoal na representação

- **WHEN** um campo do log é um objeto cuja representação contém um e-mail
- **THEN** o e-mail não aparece na saída

#### Scenario: Estrutura circular ou muito profunda

- **WHEN** um campo do log é uma estrutura circular ou com mais de 8 níveis
- **THEN** o log é escrito, com o marcador de redação no lugar do que passou do limite

#### Scenario: Correlação preservada

- **WHEN** um log tem `trace_id` em UUID e o carimbo de tempo do evento
- **THEN** os dois saem intactos

### Requirement: Recall medido e limite escrito

O projeto SHALL medir, num conjunto fixo gerado com dados sintéticos pt-BR, a proporção de valores totalmente
escondidos por tipo. O recall MUST ser de pelo menos 99% para CPF, CNPJ, cartão, telefone, e-mail, CEP e endereço com
logradouro comum e número, e um teste MUST falhar abaixo desse piso. O recall de nomes próprios e o de endereços sem
número ou de logradouro raro MUST ser medidos e publicados na documentação de segurança, como limite conhecido.

#### Scenario: Queda de recall

- **WHEN** uma mudança faz o recall de um tipo com piso cair abaixo de 99%
- **THEN** o teste falha, indicando o tipo

#### Scenario: Limite de nomes publicado

- **WHEN** a change termina
- **THEN** a documentação de segurança traz o recall medido de nomes próprios, o que ele significa e qual a melhoria
  prevista
