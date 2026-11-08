# Conjuntos de avaliação versionados

Pequenos o bastante para ficar no git, para o CI medir a qualidade sem baixar o dataset.

| Arquivo | Conteúdo | Origem |
| --- | --- | --- |
| `policy_qa.jsonl` | 60 pares pergunta → seção da política (`document`, `section`) | Conjunto `policy_qa` do dataset sintético v1.0.0, semente 42 (`docs/dados-sinteticos.md`): perguntas escritas pelo Gemini a partir de cada seção, com o filtro que recusa pergunta que copia cinco palavras seguidas do trecho |
| `out_of_scope.jsonl` | 30 perguntas fora do escopo do atendimento (clima, receitas, bancos, outros serviços) | Escritas à mão. Algumas usam palavras da política em outro contexto ("cancelar a assinatura da TV a cabo"), para testar a regra de busca não confiável |

As perguntas fora do escopo não entram na escolha do limiar de similaridade: elas medem a recusa de forma honesta
(`docs/busca-politicas.md`).
