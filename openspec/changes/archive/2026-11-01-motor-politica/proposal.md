## Why

Change 3 do `docs/roadmap.md` (`motor-politica`, fase F1), que cobre os requisitos **R12** (agente que age) e
**R22** (segurança de LLM). O Deli só pode reembolsar, dar cupom ou cancelar se uma regra **em código** permitir:
é a defesa principal contra "ignore as regras e me reembolse" e a fonte do gabarito do gerador de dados (change 6)
e dos evals. Ela vem antes do agente, das ações e da busca nas políticas, que dependem dela, e logo depois do
domínio de pedidos, que é a entrada dela.

## What Changes

Escopo copiado do roadmap:

- `policy/limits.py` como fonte única dos números (tetos, prazos, limites de autoaprovação).
- Os documentos `POL-01` a `POL-12` em markdown, em português, com marcadores (`{{auto_approval_limit}}`) no lugar
  de cada número.
- O motor `evaluate`, que devolve uma `PolicyDecision` (permitida, precisa de aprovação ou negada, com motivo e
  teto) para reembolso, cupom e cancelamento.

Detalhado a partir do escopo (sem mudar o que o roadmap pede):

- A ação proposta e o histórico que o motor recebe (`ProposedAction`, `CustomerHistory`) como modelos do domínio,
  para que a mesma função sirva ao agente, à reavaliação dentro da trava (change 11) e ao gerador de dados.
- O preenchimento dos marcadores (`render_policy_document`), função pura que recusa marcador desconhecido. Entra
  aqui para o teste provar já agora que o texto e o código não divergem; a indexação continua na change 8.
- Contrato novo do import-linter: `policy` é puro e só depende de `orders` e `shared`.
- Cobertura de ramos ligada no `pytest` e uma etapa do CI que exige 100% dos ramos em `policy`.

## Fora de escopo

- A indexação dos documentos para o RAG: trechos, embeddings e busca (change 8).
- Executar a ação, travar o pedido, gravar a auditoria e o pedido de aprovação (change 11). Aqui só se decide.
- A nota de fraude e a regra "probabilidade × valor" (change 10): o motor não recebe a nota.
- O `ActionProposal` em Pydantic que o LLM devolve e a conversão para `ProposedAction` (change 12).
- Os eventos de log das decisões: o motor é puro e não loga; quem chama registra (changes 11 e 12).
- Os casos sensíveis (alergia, conduta do entregador): o roteamento os manda para humano antes de existir uma
  proposta (change 12). O motor não tem ação para eles.
- Calibrar os números: continuam provisórios (`docs/modelo-dominio.md`, seção 3) e só mudam em `limits.py`.

## Capabilities

### New Capabilities

- `politica`: decisão determinística sobre uma ação proposta (permitida, precisa de aprovação ou negada, com motivo,
  teto e documento citável), a partir do pedido, do histórico do cliente e do instante da reclamação; números numa
  fonte única que também preenche os documentos da política.

### Modified Capabilities

Nenhuma. `pedidos` e `auditoria` não mudam de comportamento: o motor só lê o `Order` já carregado.

## Critério de pronto (do roadmap)

- 100% de cobertura de ramos em `policy` (o CI barra abaixo disso);
- cada regra de `docs/modelo-dominio.md`, seção 3, tem pelo menos um teste;
- o motor é Python puro, e o `lint-imports` garante isso (um import proibido de propósito quebra o build).

Acrescentados pela change: todo marcador dos documentos existe em `limits.py`, e nenhum documento escreve um valor
em reais à mão.

## Impact

- **Código novo:** `src/deliverius/policy/` (`limits.py`, `models.py`, `engine.py`, `documents.py`, `docs/*.md`) e
  `tests/unit/policy/`.
- **Código alterado:** `pyproject.toml` (contrato do import-linter e cobertura de ramos) e `.github/workflows/ci.yml`
  (etapa dos 100% de ramos em `policy`).
- **Dependências novas:** nenhuma. **Variáveis novas:** nenhuma; os números da política são código revisado em PR.
- **Documentação:** `docs/modelo-dominio.md`, seção 3 (o que cada regra conta e de onde, decidido aqui),
  `docs/decisoes-arquitetura.md`, `docs/glossario.md` e `docs/roadmap.md` (o preenchimento dos marcadores sai da
  change 8 e entra nesta).
