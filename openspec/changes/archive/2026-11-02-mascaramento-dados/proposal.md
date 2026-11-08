## Why

Change 4 do `docs/roadmap.md` (`mascaramento-dados`, fase F1), que cobre o requisito **R22** (segurança de LLM). O
texto do cliente vai para o LLM, o banco, o log e o trace, e nenhum deles pode receber CPF, cartão, telefone ou
endereço (LGPD, minimização). O mascaramento precisa existir antes da camada de LLM e do agente, que dependem dele
como primeira barreira, e completa o filtro do logger, que hoje só olha o nome das chaves e deixa passar o texto
livre.

## What Changes

Escopo copiado do roadmap:

- Módulo `privacy/` próprio, com reconhecimento de CPF e CNPJ com dígito verificador, cartão com Luhn, telefone,
  e-mail, CEP e endereço.
- Normalização NFKC e remoção de caracteres invisíveis antes de mascarar (`robustez.md`, seção 6).
- Marcadores tipados e estáveis na conversa (`[CPF_1]`).
- O filtro de mascaramento para o logger.

Detalhado a partir do escopo (o roadmap é atualizado junto):

- **CNPJ alfanumérico:** a Receita Federal passou a emitir CNPJ com letras em 2026, e o reconhecimento aceita os dois
  formatos. Um padrão só com dígitos deixaria esses números visíveis.
- **Normalização além do NFKC:** dígitos de outros alfabetos e variações de hífen, que o NFKC não converte e que
  driblariam o reconhecimento.
- **Correção no logger:** o texto de exceções é formatado depois da redação e chega ao log sem filtro (reproduzido
  durante esta proposta). A formatação passa para antes da redação.
- **Registro de marcadores irreversível:** para o mesmo dado receber o mesmo marcador em turnos diferentes sem
  guardar o valor, o registro guarda só uma impressão digital com chave secreta.
- Contrato novo do import-linter (`privacy` é puro) e etapa do CI que exige 100% dos ramos em `privacy`.
- `Faker` como dependência de desenvolvimento, para o conjunto de medição.

## Fora de escopo

- O filtro no exportador de traces (change 14).
- O nó `mask_input`, o uso do registro no estado do grafo e a variável de ambiente da chave (change 12).
- A rede de palavras-chave de alergia e saúde (change 12), que vai reutilizar a normalização daqui.
- A procura de dado pessoal na saída do LLM (`validate_reply`, change 12), que vai reutilizar o reconhecimento daqui.
- O eval de privacidade sobre as mensagens sintéticas rotuladas (changes 6 e 15).
- Reconhecimento de nomes próprios (NER): o limite é medido e escrito, e o NER fica como melhoria futura.
- Reversão: nada aqui recupera o valor original.

## Critério de pronto

Copiado do roadmap:

- 100% de cobertura de ramos em `privacy`.
- O recall por tipo de dado, medido num conjunto com dados do Faker pt-BR, está no relatório da change.
- O limite com nomes próprios está escrito, com o número medido.
- CPF com dígitos de largura total ou caracteres invisíveis também é mascarado.
- Nenhuma regex passa de 50 ms numa entrada de 1.000 caracteres feita para travar.

Acrescentado por esta change:

- CNPJ alfanumérico reconhecido, conferido com o exemplo oficial da Receita (`12.ABC.345/01DE-35`).
- Dado pessoal no texto de uma exceção não chega ao log, em JSON e no console, por structlog e pela biblioteca
  padrão, com um teste que falhava antes da correção.
- UUID e carimbo de tempo atravessam o filtro intactos, para os logs continuarem correlacionáveis.

## Capabilities

### New Capabilities

- `mascaramento`: normalização do texto de fora, reconhecimento de dado pessoal, marcadores estáveis e irreversíveis
  e filtro de texto livre nos logs.

### Modified Capabilities

Nenhuma.

## Impact

- **Código novo:** `src/deliverius/privacy/`, Python puro, sem I/O nem log.
- **Código alterado:** `observability/logging.py` (exceção formatada antes da redação; redator de texto livre
  genérico, sem importar `privacy`) e `api/main.py` (liga o `privacy` no logger).
- **Comportamento:** no console de desenvolvimento, a exceção passa a sair como texto simples, sem o quadro
  colorido, para ser redigida como em produção.
- **Dependências:** `faker` no grupo de desenvolvimento; nenhuma em produção.
- **Qualidade:** `pyproject.toml` (contratos do import-linter), `.github/workflows/ci.yml` (etapa de 100% dos ramos).
- **Documentação:** `seguranca.md` (seção 4, com os números), `roadmap.md`, `decisoes-arquitetura.md`, `glossario.md`,
  `robustez.md` (seção 6), `arquitetura.md` (regras de import).
- **Banco, API e grafo:** nenhuma mudança agora.
