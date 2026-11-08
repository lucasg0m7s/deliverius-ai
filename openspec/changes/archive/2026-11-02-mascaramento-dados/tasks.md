## 1. Preparação

- [x] 1.1 `uv add --dev faker`; `uv.lock` atualizado e `uv run python -c "import faker"` funciona
- [x] 1.2 Criar `src/deliverius/privacy/` e o contrato "privacy is pure"; colocar `policy` e `privacy` lado a lado nas camadas e proibir `deliverius.privacy` no contrato de utilitários, com a sintaxe conferida na documentação da versão instalada do import-linter; `uv run lint-imports` passa e falha ao importar `structlog` em `privacy` (desfeito em seguida)

## 2. Normalização

- [x] 2.1 `normalize_text` com os cinco passos do design; testes dos cenários da spec (largura total, invisíveis, árabe-índicos, travessão, espaços, quebra de linha mantida)
- [x] 2.2 Propriedades com Hypothesis: `normalize_text` é idempotente e não lança exceção em nenhum texto; `uv run pytest tests/unit/privacy` passa

## 3. Reconhecimento

- [x] 3.1 `documents.py`: dígito verificador de CPF e de CNPJ (numérico e alfanumérico) e Luhn; testes com o exemplo oficial `12ABC34501DE` → `35`, com valores do Faker e com a fronteira de resto 0 e 1 do módulo 11
- [x] 3.2 `detection.py`: padrões com repetição limitada e `[0-9]`, precedência do design e `find_personal_data`; um teste para cada cenário de reconhecimento da spec (documentos pontuados e soltos, CNPJ em minúsculas, cartão com e sem Luhn, `+55`, `0800`, e-mail com dígitos, CEP nos dois formatos, oito dígitos soltos, endereço com logradouro comum)
- [x] 3.3 Conjunto negativo em teste: preços, horários, datas, quantidades, versões, UUID, ISO 8601, `via 2`, `vale 50`, `conjunto 2`; os falsos positivos aceitos no design (`rua 12`, número de 10 dígitos) ficam em testes com nome explícito
- [x] 3.4 Teste de tempo com as entradas hostis da spec (melhor de 3 execuções, menos de 50 ms em 1.000 caracteres), incluindo `U+FDFA`; passa em `uv run pytest tests/unit/privacy`

## 4. Mascaramento

- [x] 4.1 `redact_text`: marcadores sem número e `[TEXTO_OMITIDO]` acima de 64.000 caracteres, antes e depois do NFKC; testes dos dois limites, inclusive 64.000 exatos
- [x] 4.2 `MarkerLedger` e `mask_text`: impressão digital com HMAC-SHA256, numeração por tipo e contagem em `MaskedText`; testes de estabilidade entre turnos e formatos, dois valores do mesmo tipo, conversas diferentes e registro serializado sem o CPF nem seus dígitos
- [x] 4.3 Limites do registro e da chave: 100 valores viram marcador sem número sem crescer o registro; chave com menos de 32 bytes lança `InvariantError` com código estável; testes dos dois lados da fronteira
- [x] 4.4 Propriedades com Hypothesis: mascarar duas vezes não muda o resultado; CPF válido gerado em texto aleatório nunca aparece na saída; nenhum texto faz `redact_text` ou `mask_text` lançarem exceção

## 5. Filtro do logger

- [x] 5.1 Teste de regressão escrito antes: CPF numa exceção registrada, por structlog e pela biblioteca padrão, em JSON e no console; confirmar que falha com o `configure_logging` atual e guardar a saída para o relatório
- [x] 5.2 Mover `format_exc_info` para a cadeia comum, antes da redação; o teste de 5.1 passa, e os testes existentes de `test_logging.py` continuam verdes sem serem afrouxados
- [x] 5.3 `text_redactor` em `observability/logging.py`, sem importar `privacy`: percorre mensagem, campos, aninhados e `repr` de objetos, com inteiros intactos e limite de 8 níveis; testes de cada cenário da spec, inclusive estrutura circular
- [x] 5.4 Ligar em `api/main.py` com `redact_text`; teste que sobe a aplicação com `create_app` e confere que um CPF em log não sai, e que `trace_id` em UUID e `timestamp` saem intactos
- [x] 5.5 Medir o tempo de `redact_text` numa mensagem curta e escrever o número no relatório da change; se passar de 100 µs por campo, registrar a decisão sobre o atalho em `decisoes-arquitetura.md`

## 6. Recall

- [x] 6.1 Conjunto de medição em `tests/support/` com `Faker("pt_BR")`, semente fixa e frases-modelo, sem nenhum valor real
- [x] 6.2 Teste de piso de 99% por tipo estruturado, com mensagem que indica o tipo; confirmar que falha ao desligar um padrão (desfeito em seguida)
- [x] 6.3 Medir nomes próprios, endereços com número do Faker e todos os endereços do Faker; escrever os números em `docs/seguranca.md`, seção 4, e o que eles significam

## 7. Qualidade

- [x] 7.1 Etapa nova no `ci.yml`, no modelo da etapa do `policy`, exigindo 100% dos ramos em `deliverius.privacy` com `tests/unit/privacy`; a rodada local passa com 100% e falha ao comentar um teste de ramo (desfeito em seguida)

## 8. Documentação

- [x] 8.1 `docs/seguranca.md`, seção 4: CNPJ alfanumérico, documento pontuado sem dígito válido, normalização, marcadores sem número no logger, registro com HMAC como pseudonimização, tabela de recall com o que ela mede; `docs/robustez.md`, seção 6: passos da normalização e limites
- [x] 8.2 `docs/decisoes-arquitetura.md`, com as alternativas: normalização além do NFKC; CNPJ alfanumérico; documento pontuado sem dígito válido; lista curta de logradouros; registro com HMAC por conversa; `redact_text` separado de `mask_text`; exceção formatada antes da redação; inversão de dependência no logger; limites com falha para o lado seguro
- [x] 8.3 `docs/glossario.md`: marcador, registro de marcadores (`MarkerLedger`), impressão digital, normalização; `docs/arquitetura.md`: regras de import atualizadas e `bootstrap` ainda não existente; conferir que `convencoes-codigo.md` não contradiz
- [x] 8.4 `docs/roadmap.md`: na change 4, o escopo e os critérios acrescentados; na change 12, a variável de ambiente da chave, o uso de `normalize_text` na rede de palavras-chave e de `find_personal_data` no `validate_reply`; na change 14, o uso de `redact_text` no exportador
- [x] 8.5 README: uma linha sobre o mascaramento próprio, com o recall medido dos tipos estruturados e o limite de nomes, sem apresentar os 100% como resultado em mensagens reais

## 9. Verificação

- [x] 9.1 `uv run ruff format --check .`, `uv run ruff check .`, `uv run mypy`, `uv run lint-imports`, `uv run pytest` (cobertura total ≥ 85%) e a etapa de 100% dos ramos em `privacy`
- [x] 9.2 `uv run pytest -m integration --no-cov` com o Docker ligado, conferindo "34 passed" e nenhum `skipped`: sem o Docker, a suíte pula os testes e termina com sucesso
- [x] 9.3 Checklists: `docs/robustez.md`, seção 11 (respondido no design) e `docs/seguranca.md`, seção 7; matriz de autorização sem linha nova (nenhuma rota ou ferramenta), e `git grep` do `CLAUDE.local.md` vazio
