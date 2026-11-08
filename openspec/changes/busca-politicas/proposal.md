## Why

Change 8 do `docs/roadmap.md` (`busca-politicas`, fase F2), que cobre os requisitos **R08** (pesquisa) e **R19** (RAG e
busca vetorial). O agente (change 12) precisa citar a política certa ao responder dúvidas e ao propor ações, e o
`answer_faq` só pode citar o que a busca devolveu. Sem uma busca medida, o Deli inventaria regras ou citaria o
documento errado, e a pergunta "por que busca híbrida?" ficaria sem número.

## What Changes

Escopo copiado do roadmap: `retrieval/` com:

- e5-small em ONNX;
- indexação por seção, com os números preenchidos por `render_policy_document` (change 3), pela CLI, com o hash do
  documento;
- configuração `pt_unaccent`;
- busca híbrida com RRF em SQL;
- regra de "busca não confiável";
- a mini-pesquisa com os cerca de 60 pares pergunta → trecho.

Detalhado a partir do escopo (o roadmap e `arquitetura.md` são atualizados junto):

- **ONNX oficial em vez de exportação própria:** o repositório `intfloat/multilingual-e5-small` já publica
  `onnx/model.onnx` e o `tokenizer.json` (licença MIT). A aplicação baixa esses arquivos numa revisão fixa e confere o
  SHA-256 de cada um. Assim não há PyTorch em nenhum ambiente e nenhuma conta no Hugging Face é necessária.
- **Tabela `policy_chunks`** (migration aditiva) com o texto preenchido, o hash do documento, o embedding de 384
  dimensões e a coluna `tsvector` gerada, sem índice vetorial (são cerca de 50 trechos).
- **Limiar de similaridade calibrado** com os 60 pares e um conjunto pequeno de perguntas fora do escopo, escrito à mão
  no repositório.
- **Pares de avaliação versionados no git** (os 60 da change 6 e as perguntas fora do escopo), para o CI medir sem
  baixar o dataset.
- **Porta de qualidade no CI:** recall@3 e MRR da busca híbrida com piso fixado depois da mini-pesquisa.

## Fora de escopo

Do roadmap:

- A variante com um embedding maior (`bge-m3` ou `Qwen3-Embedding-0.6B`).

Decorrentes:

- A ferramenta `search_policy` do agente, o `answer_faq` e a validação de citação na resposta (change 12). Aqui entra
  a função de busca que eles vão chamar.
- Rota de API: a busca não fica exposta por HTTP nesta change.
- Reranker e expansão de consulta por LLM.

## Critério de pronto

Copiado do roadmap:

- A tabela compara lexical, semântica e híbrida em recall@3 e MRR.
- recall@3 e MRR entram como porta no CI.
- A busca roda sem torch.

Acrescentado por esta change:

- Reindexar sem mudança nos documentos não recalcula nenhum embedding (hash do documento).
- Duas indexações disparadas juntas não duplicam nem perdem trechos (teste de integração).
- Uma pergunta fora do escopo é marcada como busca não confiável, e o limiar é medido nos dois conjuntos.
- Arquivo do modelo com hash diferente do esperado é recusado antes de carregar.

## Capabilities

### New Capabilities

- `busca-politicas`: modelo de embedding fixo e conferido, indexação idempotente por seção, busca híbrida com RRF,
  regra de busca não confiável e avaliação com porta no CI.

### Modified Capabilities

Nenhuma.

## Impact

- **Código novo:** `src/deliverius/retrieval/`, comandos `retrieval` na CLI, `tests/unit/retrieval/`,
  `tests/integration/test_retrieval_*.py`.
- **Banco:** migration `0004`, aditiva: extensão `unaccent`, configuração de busca `pt_unaccent` e tabela
  `policy_chunks`.
- **Dependências da aplicação:** `onnxruntime`, `tokenizers`, `numpy` e `huggingface-hub` (este sai do grupo
  `datagen` e vai para as dependências principais, para baixar o modelo no build da imagem).
- **Dados:** `data/eval/policy_qa.jsonl` (60 pares) e `data/eval/out_of_scope.jsonl` (perguntas fora do escopo).
- **CI:** cache do modelo (cerca de 470 MB) entre execuções; a avaliação roda no job de integração.
- **Qualidade:** contrato do import-linter: `onnxruntime` e `tokenizers` só em `retrieval` (e, na change 9, em `intent`).
- **Documentação:** `arquitetura.md`, `modelo-dominio.md`, `ml-e-avaliacao.md`, `seguranca.md`, `robustez.md`,
  `decisoes-arquitetura.md`, `glossario.md`, `roadmap.md`, `README.md` e o relatório `docs/busca-politicas.md`.
- **API e grafo:** nenhuma mudança.
- **Pendências humanas:** nenhuma.
