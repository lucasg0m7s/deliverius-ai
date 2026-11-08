## MODIFIED Requirements

### Requirement: Papéis configuráveis por ambiente

O sistema SHALL resolver todo modelo a partir de um papel (`agent`, `writer`, `customer_sim`, `judge`, `datagen`,
`datagen_holdout`). Cada papel MUST ter modelo principal configurável por variável de ambiente, reserva opcional,
timeout e teto de tokens de saída. O papel `datagen_holdout` MUST usar por padrão um provedor diferente do papel
`datagen`. A montagem MUST falhar, com uma mensagem que cite a variável, quando faltar a chave do provedor de um papel
configurado ou quando a referência do modelo for inválida. O valor da chave MUST NOT aparecer nessa mensagem.

#### Scenario: Trocar o modelo de um papel

- **WHEN** a variável do papel `writer` aponta para outro provedor e modelo
- **THEN** o papel passa a usar esse modelo, sem mudança no código de quem chama

#### Scenario: Chave faltando

- **WHEN** um papel usa o Groq e a variável da chave do Groq não existe
- **THEN** a montagem falha citando `GROQ_API_KEY`

#### Scenario: Provedor desconhecido

- **WHEN** a referência do modelo cita um provedor que não existe
- **THEN** a configuração é recusada com uma mensagem que cita a variável

#### Scenario: Gerador de teste de outra família

- **WHEN** a configuração é a padrão
- **THEN** os papéis `datagen` e `datagen_holdout` resolvem para provedores diferentes

## ADDED Requirements

### Requirement: Chamada com saída estruturada

O sistema SHALL oferecer uma chamada que recebe um papel, um esquema de saída e as mensagens, e devolve, como resultado
tipado, o objeto validado contra o esquema ou o modo degradado. Saída que não valida contra o esquema MUST resultar no
modo degradado com um motivo próprio, sem lançar exceção ao chamador. A chamada MUST passar pelas mesmas novas
tentativas, reserva, orçamento, registro de uso e cache das demais. O conteúdo da saída inválida MUST NOT aparecer em
log.

#### Scenario: Saída válida

- **WHEN** o modelo devolve um objeto que valida contra o esquema
- **THEN** o resultado é o objeto tipado, e uma linha de uso é gravada

#### Scenario: Saída inválida

- **WHEN** o modelo devolve um objeto com um campo obrigatório ausente
- **THEN** o resultado é o modo degradado com o motivo de saída inválida, e o log não traz o conteúdo

#### Scenario: Reserva com saída estruturada

- **WHEN** o principal falha com erro passageiro em todas as tentativas e há reserva configurada
- **THEN** a reserva responde com o objeto validado
