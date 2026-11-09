## Purpose

Encontrar, com qualidade medida, o trecho da política do Deliverius que responde a uma pergunta, combinando busca por
palavra e busca por significado, e dizer quando a busca não é confiável para o agente não inventar uma regra.

## ADDED Requirements

### Requirement: Modelo de embedding fixo e conferido

O sistema SHALL gerar embeddings com o `multilingual-e5-small` em ONNX, sem PyTorch, a partir de arquivos de uma revisão
fixa do repositório oficial. Cada arquivo do modelo MUST ter o SHA-256 conferido contra o valor registrado no código
antes de ser carregado, e um arquivo divergente ou ausente MUST ser recusado com erro que cite o arquivo. O texto MUST
receber o prefixo `query: ` na consulta e `passage: ` no trecho, ser truncado no limite de tokens do modelo e o vetor
MUST ser normalizado (norma 1). O modelo MUST NOT ser baixado em execução da aplicação: o download é um comando
explícito.

#### Scenario: Arquivo adulterado

- **WHEN** o arquivo `model.onnx` local tem um SHA-256 diferente do registrado
- **THEN** o carregamento falha citando o arquivo, antes de qualquer embedding

#### Scenario: Vetor normalizado

- **WHEN** um texto é transformado em embedding
- **THEN** o vetor tem 384 dimensões e norma 1, com tolerância de 1e-5

#### Scenario: Paráfrase mais próxima que assunto diferente

- **WHEN** são comparadas "meu lanche chegou frio" com "o pedido veio gelado" e com "como troco minha senha"
- **THEN** a paráfrase tem similaridade maior que a frase de outro assunto

### Requirement: Indexação idempotente por seção

O sistema SHALL indexar os documentos de política por seção de título, com os números preenchidos a partir dos limites
da política, guardando para cada trecho um ID citável (`POL-03#prazo`), o título do documento e da seção no próprio
texto, o embedding e o hash do documento. Reindexar MUST recalcular só os documentos cujo hash mudou e MUST remover os
trechos de documentos ou seções que deixaram de existir. Os embeddings MUST ser calculados antes de abrir a transação
que grava os trechos. Duas indexações simultâneas MUST NOT duplicar nem perder trechos.

#### Scenario: Reindexar sem mudança

- **WHEN** a indexação roda duas vezes sem mudança nos documentos
- **THEN** a segunda não calcula nenhum embedding e não altera nenhuma linha

#### Scenario: Limite da política mudou

- **WHEN** o valor de autoaprovação muda e a indexação roda de novo
- **THEN** os trechos dos documentos que citam esse valor são regravados com o novo número, e os demais não

#### Scenario: Indexações juntas

- **WHEN** duas indexações são disparadas ao mesmo tempo sobre o banco vazio
- **THEN** cada trecho existe exatamente uma vez ao fim

#### Scenario: ID citável

- **WHEN** a seção "Prazo" do documento POL-03 é indexada
- **THEN** o trecho tem o ID `POL-03#prazo` e o texto começa com o título do documento e da seção

### Requirement: Busca híbrida com RRF

O sistema SHALL buscar trechos por duas listas, a lexical (full-text em português sem acento, `pt_unaccent`, com os
termos da consulta unidos por OU e ordenados por relevância) e a semântica (similaridade de cosseno com o embedding da consulta), com os 5 primeiros de cada uma, e combiná-las por
Reciprocal Rank Fusion com k = 60, devolvendo os 3 melhores com o ID, o texto, a posição em cada lista e a
similaridade. A consulta MUST ser normalizada, limitada em tamanho e convertida em consulta de texto sem interpretar
operadores escritos pelo usuário. A busca MUST oferecer também os modos só lexical e só semântica, para a avaliação.

#### Scenario: Palavra sem acento

- **WHEN** a consulta é "nao chegou meu pedido"
- **THEN** a lista lexical encontra trechos com "não chegou"

#### Scenario: Operadores na consulta

- **WHEN** a consulta contém `!`, `&`, `|`, `:*` ou aspas desbalanceadas
- **THEN** a busca responde normalmente, sem erro de sintaxe

#### Scenario: Consulta longa

- **WHEN** a consulta passa do tamanho máximo
- **THEN** ela é recusada com erro de validação, sem consultar o banco

### Requirement: Busca não confiável

O sistema SHALL marcar o resultado como não confiável quando a maior similaridade semântica fica abaixo de um limiar
configurado. A regra e o limiar MUST vir da mini-pesquisa: o maior limiar que mantém pelo menos 95% dos pares
confiáveis, com o valor, a taxa de recusa fora do escopo e a regra descartada registrados no relatório. A regra MUST
ser configurável entre "só similaridade" e "trecho nas duas listas e similaridade", para a avaliação comparar as duas.

#### Scenario: Conjunto fora do escopo

- **WHEN** as perguntas fora do escopo versionadas são buscadas com a regra e o limiar padrão
- **THEN** pelo menos o piso registrado (58%) é marcado como não confiável, e pelo menos 95% dos pares continuam
  confiáveis

#### Scenario: Pergunta da política

- **WHEN** a consulta é "quanto tempo tenho pra reclamar de item faltando"
- **THEN** o resultado é confiável e traz o trecho `POL-03#prazo` entre os 3 primeiros

### Requirement: Avaliação com porta de qualidade

O sistema SHALL avaliar os modos lexical, semântico e híbrido em recall@3 e MRR sobre os pares pergunta → seção
versionados no repositório, e a taxa de busca não confiável sobre esses pares e sobre perguntas fora do escopo. Um
acerto MUST ser contado quando um dos 3 primeiros trechos é da mesma seção do par. O CI MUST falhar quando o recall@3
ou o MRR da busca híbrida ficar abaixo do piso registrado.

#### Scenario: Porta no CI

- **WHEN** uma mudança faz o recall@3 da busca híbrida cair abaixo do piso
- **THEN** o teste de avaliação falha com os valores medido e esperado

#### Scenario: Acerto por documento

- **WHEN** a avaliação roda
- **THEN** ela também mede recall@3 e MRR considerando acerto qualquer trecho do documento certo

#### Scenario: Tabela comparativa

- **WHEN** a avaliação roda
- **THEN** ela imprime recall@3 e MRR de cada modo, com intervalo de confiança de 95% por bootstrap, e a taxa de busca
  não confiável nos dois conjuntos

### Requirement: Eventos de busca sem texto

O sistema SHALL registrar cada busca com o modo, a latência, o número de trechos, os IDs devolvidos e se foi confiável,
e cada indexação com os documentos recalculados, mantidos e removidos. O texto da consulta MUST NOT aparecer em log.

#### Scenario: Log sem a consulta

- **WHEN** uma busca é feita com um texto conhecido
- **THEN** o log traz o evento com os IDs devolvidos e não traz o texto da consulta
