## 1. Preparação

- [x] 1.1 `onnxruntime`, `tokenizers`, `numpy` e `huggingface-hub` nas dependências da aplicação (`huggingface-hub` sai do grupo `datagen`); `uv.lock` atualizado, `pip-audit` sem nada novo; teste de que importar `deliverius.retrieval` não carrega `torch` nem `transformers`
- [x] 1.2 Criar `src/deliverius/retrieval/`; import-linter: `retrieval` na camada de capacidades, contrato "onnxruntime e tokenizers só em retrieval", e `retrieval` nas fontes dos contratos de provedor de LLM e de `langchain_core`; `lint-imports` passa
- [x] 1.3 `RETRIEVAL_MODEL_DIR`, `RETRIEVAL_MIN_SIMILARITY` e `RETRIEVAL_THREADS` no `Settings` e no `.env.example`, com testes; `models/` já está no `.gitignore` (conferir)

## 2. Modelo

- [x] 2.1 `model_files.py`: revisão, nomes, tamanhos e SHA-256 (o do `tokenizer.json` e do `config.json` calculados no primeiro download e fixados no código); download com `hf_hub_download(revision=...)` e conferência; testes com arquivos falsos em `tmp_path`: hash divergente recusado citando o arquivo, arquivo ausente, download pulado quando os arquivos conferem
- [x] 2.2 `embedder.py`: `E5Embedder` com prefixos, truncamento em 512 tokens, média com máscara e normalização L2, sessão única; testes unitários com uma sessão ONNX falsa (forma, máscara, normalização, prefixo aplicado) e teste marcado `integration` com o modelo real: 384 dimensões, norma 1, paráfrase mais próxima que outro assunto

## 3. Banco

- [x] 3.1 Migration `0004`: `unaccent`, configuração `pt_unaccent`, tabela `policy_chunks` com `tsv` gerado, GIN e índice em `document_id`, `lock_timeout`; `db/tables.py`; o teste de migrations faz ida e volta
- [x] 3.2 Integração: `to_tsvector('pt_unaccent', 'não chegou')` casa com a consulta `nao chegou`; `EXPLAIN` da busca lexical usa o GIN

## 4. Indexação

- [ ] 4.1 `chunking.py`: trechos por seção a partir de `render_policy_document`, ID citável com slug, título no texto, hash do documento renderizado, título repetido recusado; testes, inclusive `POL-03#prazo`
- [ ] 4.2 `repository.py` e `indexer.py`: hashes, troca dos trechos por documento numa transação com `pg_advisory_xact_lock` e `lock_timeout`, embeddings fora da transação, releitura dos hashes dentro da trava, remoção de documentos e seções que sumiram; testes unitários com repositório em memória: segunda execução sem embedding, limite alterado reindexa só os documentos que o citam
- [ ] 4.3 Integração, corrida: duas indexações com `asyncio.gather` no banco vazio → cada trecho exatamente uma vez; e com um documento mudado entre as duas

## 5. Busca

- [ ] 5.1 `search.py`: consulta normalizada, limite de 500 caracteres, modos lexical, semântico e híbrido, RRF k = 60 em SQL com desempate por `chunk_id`, `SearchResult` com IDs, texto, posições e similaridade; regra de confiança; testes unitários da regra e da validação
- [ ] 5.2 Integração: palavra sem acento, operadores (`!`, `&`, `|`, `:*`, aspas) sem erro, resultado determinístico, pergunta da política com `POL-03#prazo` entre os 3, pergunta fora do escopo não confiável, `EXPLAIN` registrado
- [ ] 5.3 Eventos de log de `design.md` e teste de que o texto da consulta não aparece nos logs

## 6. CLI e avaliação

- [ ] 6.1 `cli/retrieval.py`: `fetch-model`, `index` e `evaluate`, com textos em português e códigos de saída como os do `datagen`; testes dos argumentos e códigos de saída
- [ ] 6.2 `data/eval/policy_qa.jsonl` (os 60 pares da v1.0.0), `data/eval/out_of_scope.jsonl` (≈ 30 perguntas fora do escopo escritas à mão) e `data/eval/README.md` com a origem
- [ ] 6.3 `evaluation.py`: recall@3, MRR, bootstrap de 1.000 com semente fixa, taxa de não confiável nos dois conjuntos, latência p50/p95 de 100 consultas; testes unitários das métricas com casos feitos à mão
- [ ] 6.4 Mini-pesquisa: rodar `evaluate`, escolher o limiar pela regra do design, fixar o padrão de `RETRIEVAL_MIN_SIMILARITY` e o piso da porta; relatório `docs/busca-politicas.md` com a tabela, o limiar, os erros comentados e os limites
- [ ] 6.5 Porta no CI: teste de integração com o modelo real que exige recall@3 e MRR da híbrida acima do piso; `actions/cache` do modelo no job de qualidade com a chave da revisão e passo `fetch-model` antes dos testes de integração

## 7. Documentação

- [ ] 7.1 `arquitetura.md` (ONNX oficial, tabela, SQL, limiar), `modelo-dominio.md` (`policy_chunks`), `ml-e-avaliacao.md` (porta da busca), `seguranca.md` (revisão e SHA-256 do modelo), `robustez.md` (trava da indexação), `autorizacao.md` (políticas sem `AccessScope`), `glossario.md`, `roadmap.md` e `decisoes-arquitetura.md`
- [ ] 7.2 README: tabela lexical × semântica × híbrida com recall@3, MRR e latência, e o link para o relatório

## 8. Verificação

- [ ] 8.1 `uv run ruff format --check .` e `uv run ruff check .`
- [ ] 8.2 `uv run mypy`
- [ ] 8.3 `uv run lint-imports`
- [ ] 8.4 `uv run pytest` (cobertura mínima de 85%)
- [ ] 8.5 `uv run pytest -m integration --no-cov`, conferindo o número de testes passados
- [ ] 8.6 `openspec validate busca-politicas --strict` e o `git grep` do `CLAUDE.local.md` vazio
- [ ] 8.7 CI verde no GitHub depois do push
