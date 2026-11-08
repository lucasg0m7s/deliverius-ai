# auditoria Specification

## Purpose

Guarda quem fez o quê, quando e sobre qual objeto, de um jeito que nem um bug da aplicação consegue reescrever ou
apagar; é a base para auditar reembolsos, cupons, cancelamentos e decisões de aprovação.

## Requirements

### Requirement: Registro de auditoria só de inserção
O registro de auditoria SHALL aceitar apenas inserções. O banco MUST recusar qualquer alteração ou remoção de uma
linha, independentemente do código que a tente.

#### Scenario: Inserção
- **WHEN** a aplicação registra uma entrada de auditoria
- **THEN** a entrada é gravada com ator, ação, alvo, dados, instante e sandbox

#### Scenario: Tentativa de alteração
- **WHEN** qualquer comando tenta alterar uma entrada de auditoria
- **THEN** o banco recusa o comando com erro e a entrada continua igual

#### Scenario: Tentativa de remoção
- **WHEN** qualquer comando tenta apagar uma entrada de auditoria
- **THEN** o banco recusa o comando com erro e a entrada continua lá

### Requirement: Auditoria sem dado pessoal
Uma entrada de auditoria SHALL identificar pessoas e objetos só por IDs e papéis. Os dados da entrada MUST NOT
conter dado pessoal (nome, CPF, telefone, e-mail, endereço), para que o registro possa ser guardado pelo prazo de
retenção sem precisar ser alterado.

#### Scenario: Dados com campo pessoal
- **WHEN** se tenta registrar uma entrada cujos dados contêm um campo de dado pessoal conhecido
- **THEN** o registro é recusado com erro antes de chegar ao banco

### Requirement: Auditoria na mesma transação da operação
O registro de auditoria SHALL ser gravado na mesma transação da operação que ele descreve: ou os dois são gravados,
ou nenhum.

#### Scenario: Operação desfeita
- **WHEN** a transação que contém a operação e a entrada de auditoria é desfeita
- **THEN** nenhuma das duas fica gravada
