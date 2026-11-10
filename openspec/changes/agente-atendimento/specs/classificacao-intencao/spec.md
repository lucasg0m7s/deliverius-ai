## Purpose

Dizer a intenção de cada mensagem do cliente, com uma confiança calibrada que decide se o Deli age ou pergunta, e
garantir que um relato sensível (saúde, segurança, assédio) vá para uma pessoa mesmo quando o modelo erra.

## ADDED Requirements

### Requirement: Onze intenções com nomes estáveis
O sistema SHALL reconhecer exatamente estas 11 intenções, com estes códigos: `item_missing`, `wrong_item`,
`quality_issue`, `late_delivery`, `not_delivered`, `billing_issue`, `cancel_order`, `coupon_question`,
`policy_question`, `sensitive` e `other`. Os códigos MUST ser os mesmos no gerador de dados, na pesquisa e na
aplicação, a partir de uma única definição.

#### Scenario: Mesma lista em todo lugar
- **WHEN** o gerador de dados e a aplicação listam as intenções
- **THEN** as duas listas são idênticas, na mesma ordem

### Requirement: Classificação com confiança e limiar
A classificação de uma mensagem SHALL receber o texto já mascarado e devolver a intenção mais provável, a
probabilidade de cada uma das 11 intenções (somando 1) e a confiança, que é a maior probabilidade. A mensagem MUST
ser considerada **incerta** quando a confiança ficar abaixo do limiar do classificador, que acompanha o modelo
(foi escolhido na validação junto com ele); o limiar MUST estar entre 0 e 1, e um valor fora disso MUST ser recusado
ao montar o classificador, impedindo a subida da aplicação.

#### Scenario: Confiança acima do limiar
- **WHEN** o classificador dá 0,94 para `item_missing` e o limiar é 0,60
- **THEN** a classificação traz `item_missing`, confiança 0,94 e não é incerta

#### Scenario: Confiança abaixo do limiar
- **WHEN** o classificador dá 0,38 para a intenção mais provável e o limiar é 0,60
- **THEN** a classificação é incerta e traz as duas intenções mais prováveis

#### Scenario: Limiar inválido
- **WHEN** um classificador é montado com limiar 1,5
- **THEN** a montagem falha com um erro que cita o limiar

### Requirement: Rede de segurança por palavras-chave
Antes do classificador, o sistema SHALL procurar, no texto normalizado (sem acento, sem caractere invisível,
minúsculo), termos de saúde, segurança e assédio (por exemplo alergia, alérgico, passando mal, vomitei, intoxicação,
acidente, agressão, assédio). Achado um termo, a intenção MUST ser `sensitive`, qualquer que seja a nota do
classificador, e o resultado MUST registrar que a rede de segurança decidiu. A lista MUST ser fixa no código, com
teste para cada termo e para variações de acento, maiúscula e caractere invisível.

#### Scenario: Classificador erra e a rede corrige
- **WHEN** a mensagem é "veio com amendoim e eu avisei que sou alérgico, tô passando mal" e o classificador diz
  `wrong_item` com 0,91
- **THEN** a intenção é `sensitive`, marcada como decidida pela rede de segurança

#### Scenario: Acento e caractere invisível não escondem o termo
- **WHEN** a mensagem traz "ALÉR​GICO" com um caractere de largura zero no meio
- **THEN** a rede de segurança reconhece o termo e a intenção é `sensitive`

#### Scenario: Frase comum não dispara a rede
- **WHEN** a mensagem é "o pedido chegou frio"
- **THEN** a rede de segurança não decide e vale a intenção do classificador

### Requirement: Classificação local, sem LLM externo
A classificação SHALL rodar no processo da aplicação, sem chamada a nenhum provedor de LLM, para que um relato de
saúde nunca saia do sistema antes de chegar a uma pessoa.

#### Scenario: Nenhuma chamada de rede ao classificar
- **WHEN** uma mensagem sensível é classificada
- **THEN** nenhum modelo de LLM é chamado
