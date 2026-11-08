## Context

Motivação em `proposal.md`, requisitos em `specs/busca-politicas/spec.md`. A busca híbrida (lexical `pt_unaccent` +
pgvector + RRF k = 60, 5 de cada lado, 3 para o LLM, sem índice vetorial) e a regra de busca não confiável já estão em
`docs/arquitetura.md`, seção "Busca nas políticas", e não são rediscutidas aqui.

O estado atual que molda o desenho:

- Os 12 documentos (`policy/docs/`) têm só títulos de nível 2, quatro por documento: 48 seções. `render_policy_document`
  preenche os marcadores com `POLICY_LIMITS`.
- A change 6 gerou 60 pares pergunta → (documento, título da seção), com um filtro que recusa pergunta que copia cinco
  palavras seguidas da seção.
- O banco tem a extensão `vector` (migration 0001). O `unaccent` vem no `contrib` da imagem `pgvector/pgvector:pg17` e
  existe no Neon.
- `huggingface-hub` está só no grupo `datagen`; `onnxruntime`, `tokenizers` e `numpy` não estão no projeto.
- **Conferido em 2026-11-08** na API do Hugging Face: `intfloat/multilingual-e5-small`, revisão
  `614241f622f53c4eeff9890bdc4f31cfecc418b3`, licença MIT, com `onnx/model.onnx` (470.268.510 bytes, SHA-256
  `ca456c06…6bc8665`), `onnx/tokenizer.json` (17.082.730 bytes) e `onnx/config.json`. Há também `model_O4.onnx` (fp16,
  pensado para GPU) e uma versão int8 que exige AVX-512 VNNI; nenhuma das duas serve a uma CPU qualquer.

## Goals / Non-Goals

**Goals:**

- Uma função de busca que o agente (change 12) chama sem conhecer SQL nem ONNX.
- Números que respondem "por que busca híbrida?" e uma porta que impede regressão.
- Nenhum PyTorch e nenhuma rede em execução da aplicação.

**Non-Goals:**

- Embedding maior, reranker ou expansão de consulta.
- Busca exposta por HTTP.

## Decisions

### Estrutura

```
src/deliverius/retrieval/
  __init__.py      API pública
  model_files.py   Revisão, nomes, tamanhos e SHA-256 dos arquivos; download e conferência
  embedder.py      E5Embedder: tokenizers + onnxruntime, prefixos, truncamento, média com máscara, normalização
  chunking.py      Documento renderizado → trechos por seção (ID citável, título no texto, hash do documento)
  repository.py    policy_chunks: leitura dos hashes, troca dos trechos de um documento, busca em SQL
  indexer.py       Reindexação incremental com trava
  search.py        PolicySearch: modos, RRF, regra de confiança, SearchResult
  evaluation.py    recall@3, MRR, bootstrap, taxa de não confiável
src/deliverius/cli/retrieval.py   `python -m deliverius.cli retrieval fetch-model | index | evaluate`
```

API pública: `E5Embedder.load(model_dir)`, `PolicyIndexer(...).reindex()`, `PolicySearch(...).search(query, mode)`
→ `SearchResult(chunks, reliable)`, `evaluate(...)`.

### Modelo: ONNX oficial numa revisão fixa

`fetch-model` baixa os três arquivos com `hf_hub_download(revision=<sha>)` para `models/multilingual-e5-small/`
(fora do git) e confere tamanho e SHA-256 de cada um contra `model_files.py`; `E5Embedder.load` confere de novo antes
de abrir a sessão. O Dockerfile (change de entrega) roda `fetch-model` no build, então a aplicação nunca acessa a rede
para o modelo.

Embedding: tokenização com o `tokenizer.json` (truncamento em 512 tokens), sessão ONNX na CPU com
`intra_op_num_threads` configurável, média da saída `last_hidden_state` ponderada pela máscara de atenção e
normalização L2. É o que o cartão do modelo prescreve. Os prefixos `query: ` e `passage: ` são aplicados pelo
`E5Embedder`, nunca por quem chama.

**Alternativas descartadas:**

- *Exportar por nós com `optimum`* (o que a arquitetura previa): exigiria PyTorch no ambiente de pesquisa e uma conta
  no Hugging Face para publicar o artefato, para obter o mesmo arquivo que o autor já publica.
- *`model_O4.onnx` ou a versão int8:* fp16 é pensado para GPU; o int8 publicado depende de AVX-512 VNNI. Quantizar por
  nós fica para quando a latência pedir, medida.
- *`sentence-transformers` ou `fastembed`:* trazem PyTorch ou tiveram problemas com o ONNX do E5 (`arquitetura.md`).

### Tabela e configuração de busca (migration 0004)

```sql
CREATE EXTENSION IF NOT EXISTS unaccent;
CREATE TEXT SEARCH CONFIGURATION pt_unaccent (COPY = portuguese);
ALTER TEXT SEARCH CONFIGURATION pt_unaccent
  ALTER MAPPING FOR hword, hword_part, word WITH unaccent, portuguese_stem;

CREATE TABLE policy_chunks (
  chunk_id      text PRIMARY KEY,            -- 'POL-03#prazo'
  document_id   text NOT NULL,               -- 'POL-03'
  section       text NOT NULL,               -- título da seção
  position      smallint NOT NULL,           -- ordem no documento
  content       text NOT NULL,               -- título do documento e da seção + corpo preenchido
  document_hash text NOT NULL,               -- SHA-256 do documento renderizado
  embedding     vector(384) NOT NULL,
  tsv           tsvector GENERATED ALWAYS AS (to_tsvector('pt_unaccent', content)) STORED,
  indexed_at    timestamptz NOT NULL
);
CREATE INDEX policy_chunks_tsv ON policy_chunks USING gin (tsv);
CREATE INDEX policy_chunks_document ON policy_chunks (document_id);
```

- O hash é do documento **renderizado**: mudar um limite em `limits.py` muda o texto e força a reindexação só dos
  documentos que citam aquele limite.
- O slug da seção é o título em minúsculas, sem acento, com hífens; dois títulos iguais no mesmo documento são
  recusados na indexação.
- `to_tsvector` com o nome da configuração literal é imutável, como a coluna gerada exige.
- Sem índice vetorial, de propósito (48 trechos). O limite (um HNSW quando passar de alguns milhares) fica anotado.

### Indexação incremental com trava

1. Renderiza os 12 documentos, divide em trechos e calcula o hash de cada documento (sem banco).
2. Lê `document_id → document_hash` do banco (uma consulta).
3. Calcula os embeddings só dos documentos novos ou mudados, **fora de transação** (ONNX leva centenas de ms).
4. Numa transação curta com `pg_advisory_xact_lock(<constante>)` e `lock_timeout`: para cada documento mudado, apaga e
   insere os trechos; apaga os documentos que não existem mais. Dentro da trava, relê os hashes: se outra indexação já
   gravou o mesmo hash, pula (sem trabalho duplicado nem conflito de chave).

**Alternativas descartadas:** *`INSERT … ON CONFLICT` por trecho, sem trava* (deixaria trechos órfãos de uma seção
removida, se as duas execuções se cruzassem); *recalcular tudo sempre* (simples, mas o critério de pronto pede hash, e
o custo cresce com a política).

### Busca em SQL

Uma consulta só, com duas CTEs limitadas a 5 e a fusão:

```sql
WITH lexical AS (
  SELECT chunk_id, row_number() OVER (ORDER BY ts_rank_cd(tsv, q) DESC, chunk_id) AS rank
  FROM policy_chunks, websearch_to_tsquery('pt_unaccent', :query) q
  WHERE tsv @@ q ORDER BY rank LIMIT 5),
semantic AS (
  SELECT chunk_id, 1 - (embedding <=> :vector) AS similarity,
         row_number() OVER (ORDER BY embedding <=> :vector, chunk_id) AS rank
  FROM policy_chunks ORDER BY rank LIMIT 5)
SELECT c.chunk_id, c.content, l.rank, s.rank, s.similarity,
       coalesce(1.0 / (60 + l.rank), 0) + coalesce(1.0 / (60 + s.rank), 0) AS rrf
FROM lexical l FULL JOIN semantic s USING (chunk_id) JOIN policy_chunks c USING (chunk_id)
ORDER BY rrf DESC, c.chunk_id LIMIT 3;
```

- `websearch_to_tsquery` aceita texto livre e nunca dá erro de sintaxe, ao contrário de `to_tsquery`; o desempate por
  `chunk_id` deixa o resultado determinístico.
- Os modos lexical e semântico usam a CTE correspondente sozinha.
- A consulta passa por `normalize_text` e é recusada acima de 500 caracteres. O texto chega ao banco como parâmetro,
  nunca concatenado. A consulta que o agente manda já vem mascarada (change 12).
- A busca abre uma sessão curta, só de leitura, com o `statement_timeout` já configurado no engine.

### Regra de confiança e limiar

`reliable = (algum trecho aparece nas duas listas) and (maior similaridade ≥ limiar)`. O limiar é escolhido na
mini-pesquisa como o maior valor que mantém pelo menos 95% dos 60 pares confiáveis, e o relatório mostra quantas
perguntas fora do escopo ele recusa. O valor vai para a configuração (`RETRIEVAL_MIN_SIMILARITY`, com o padrão
medido). Como os 60 pares servem para escolher o limiar e para medi-lo, o número de confiabilidade nos pares é
otimista; as perguntas fora do escopo não entram na escolha e dão a medida honesta da recusa.

### Avaliação e porta no CI

- `data/eval/policy_qa.jsonl`: os 60 pares da change 6 (versão 1.0.0), copiados para o git com a origem no cabeçalho
  do README da pasta. `data/eval/out_of_scope.jsonl`: cerca de 30 perguntas fora do escopo escritas à mão (clima,
  receita, conta bancária, assunto de outro app), que não dependem de LLM.
- Acerto: um dos 3 primeiros trechos tem o mesmo documento e o mesmo título de seção do par. MRR sobre os 3 primeiros
  (0 se não aparece).
- Intervalo de 95% por bootstrap (1.000 reamostragens, semente fixa).
- **Porta:** um teste de integração indexa no Postgres do Testcontainers com o modelo real e exige recall@3 e MRR da
  híbrida acima do piso. O piso é o valor medido menos uma folga de 0,05, arredondado, registrado no teste e no
  relatório. O CI guarda o modelo em `actions/cache` com a chave da revisão; sem o modelo, o teste falha (não pula),
  como os demais testes de integração.

### Dependências e fronteiras

- `onnxruntime`, `tokenizers`, `numpy` e `huggingface-hub` nas dependências da aplicação (este último sai do grupo
  `datagen`). Sem `torch`, `transformers` nem `optimum`; um teste confere que importar `deliverius.retrieval` não
  carrega `torch`.
- import-linter: `retrieval` na camada de capacidades (pode importar `policy`, `privacy`, `db`, `shared`); contrato
  novo "onnxruntime e tokenizers só em `retrieval`" (a change 9 acrescenta `intent`).

## Checklist de `docs/robustez.md`, seção 11

| Item | Resposta |
| --- | --- |
| Rota, ferramenta, `AccessScope` | Nenhuma rota nem ferramenta nesta change. As políticas são públicas (matriz de `autorizacao.md`), como o catálogo de restaurantes: o repositório não recebe `AccessScope`, e a exceção fica escrita na docstring e em `autorizacao.md` |
| Corrida | Duas indexações juntas: trava consultiva na transação, releitura dos hashes dentro dela; teste de integração com `asyncio.gather`. A busca só lê |
| Idempotência | Reindexar é idempotente pelo hash; repetir `fetch-model` com os arquivos certos não baixa de novo |
| Timeout e novas tentativas | Download: timeout do `huggingface-hub`, sem nova tentativa automática (o comando pode ser repetido). Busca: `statement_timeout` do engine. Embedding: local, limitado pelo truncamento em 512 tokens |
| Recursos esgotáveis | Consulta até 500 caracteres; 5 trechos por lado; sessão ONNX única por processo, com threads configuráveis; modelo de 470 MB carregado uma vez |
| Queda no meio | Indexação: embeddings antes da transação; a transação troca os trechos por documento de uma vez, então uma queda deixa o estado anterior inteiro. Download: arquivo parcial falha na conferência do hash e é baixado de novo |
| Transação com rede ou LLM | Nenhuma: o ONNX roda antes da transação, e não há LLM |
| Eventos de log | `retrieval.model_verified`, `retrieval.model_rejected` (arquivo), `retrieval.indexed` (recalculados, mantidos, removidos), `retrieval.search` (modo, latência, IDs, confiável), `retrieval.search_unreliable`. A taxa de busca não confiável vira métrica no E1. Nunca o texto da consulta |
| Texto de fora e regex | Consulta normalizada, limitada e passada como parâmetro a `websearch_to_tsquery`. Os documentos são nossos. Nenhuma regex nova sobre texto de cliente além do slug do título, que roda só sobre os documentos |
| Grafo, banco, API | Migration aditiva (extensão, configuração e tabela novas), com `downgrade` testado; grafo e API não mudam |
| Consultas e índices | GIN no `tsv`, índice em `document_id`; a busca semântica é varredura exata de propósito (48 linhas), conferida com `EXPLAIN` no teste e anotada para quando crescer. `LIMIT` nas duas CTEs |
| Travas e migrations | Uma trava consultiva só, com `lock_timeout`. A migration cria objetos novos, sem trava em tabela com dados |
| Configuração nova | `RETRIEVAL_MODEL_DIR` (padrão `models/multilingual-e5-small`), `RETRIEVAL_MIN_SIMILARITY` (padrão medido) e `RETRIEVAL_THREADS`. Modelo ausente ou adulterado falha no carregamento com o nome do arquivo |

## Checklist de `docs/seguranca.md`, seção 7

- Cadeia de suprimentos (LLM03): revisão fixa por hash do commit e SHA-256 de cada arquivo; ONNX não executa código ao
  carregar, ao contrário de um pickle.
- O texto indexado é nosso; o que o agente recebe da busca entra no prompt como dado delimitado (change 12).
- A consulta nunca vai para log.

## Avaliação

| O quê | Métrica | Limiar |
| --- | --- | --- |
| Busca híbrida nos 60 pares | recall@3 e MRR, com intervalo de 95% | Piso no CI = medido − 0,05 |
| Lexical e semântica sozinhas | recall@3 e MRR | Relatado, para a comparação |
| Regra de confiança | Fração dos pares confiáveis; fração das perguntas fora do escopo recusadas | ≥ 95% dos pares confiáveis (por construção do limiar); a recusa fora do escopo é relatada |
| Latência | p50 e p95 da busca completa (embedding + SQL) em CPU, 100 consultas | Relatada; meta p95 ≤ 100 ms |

O resultado fica em `docs/busca-politicas.md` e resumido no README.

## Risks / Trade-offs

- [Os 60 pares foram gerados por LLM a partir das próprias seções, então favorecem a busca] → O filtro de cinco
  palavras da change 6 reduz a cópia literal; o relatório diz o limite, e os evals do agente (change 15) medem a busca
  com perguntas de clientes simulados.
- [Com 48 trechos curtos, recall@3 pode saturar perto de 1] → O MRR e a comparação entre modos continuam informativos;
  se saturar, o relatório diz isso em vez de inflar a conclusão.
- [470 MB no CI] → `actions/cache` com a chave da revisão; o primeiro download leva cerca de um minuto.
- [O dicionário `portuguese_stem` junta palavras demais ou de menos] → Medido pelo modo lexical na tabela.
