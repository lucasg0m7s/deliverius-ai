# Relatório da busca nas políticas

Resultado da change `busca-politicas` (mini-pesquisa). O desenho está em `arquitetura.md`, seção "Busca nas
políticas". Medido em 2026-11-08 com o `multilingual-e5-small` em ONNX (revisão `614241f6`), 48 trechos (12
documentos × 4 seções), os 60 pares de `data/eval/policy_qa.jsonl` e as 30 perguntas fora do escopo de
`data/eval/out_of_scope.jsonl`. Intervalos de 95% por bootstrap (1.000 reamostragens). Reproduzir:

```bash
uv run python -m deliverius.cli retrieval fetch-model
uv run python -m deliverius.cli retrieval index
uv run python -m deliverius.cli retrieval evaluate
```

## Resultado

| Modo | recall@3 (seção) | MRR@3 (seção) | recall@3 (documento) | MRR@3 (documento) |
| --- | --- | --- | --- | --- |
| Lexical (`pt_unaccent`, termos com OU) | 0,350 [0,217; 0,467] | 0,206 [0,125; 0,289] | 0,517 [0,383; 0,650] | 0,403 [0,292; 0,514] |
| Semântica (e5-small) | 0,550 [0,433; 0,667] | 0,431 [0,328; 0,536] | 0,700 [0,567; 0,817] | 0,594 [0,478; 0,703] |
| **Híbrida (RRF k = 60)** | **0,550 [0,417; 0,683]** | **0,414 [0,306; 0,525]** | **0,717 [0,600; 0,833]** | **0,608 [0,497; 0,719]** |

Latência da busca completa (embedding da consulta e SQL), 100 consultas uma por vez em CPU: **p50 24 ms, p95 29 ms**.

**Leitura honesta:** a híbrida empata com a semântica por seção e fica um pouco à frente por documento, mas os
intervalos se sobrepõem: com 60 pares, os dados não provam que a híbrida é melhor. Ela fica porque cobre o que os
pares não medem: termos exatos e números ("estorno", "R$ 50", "POL-07"), que a lexical acha e a semântica dilui. E os
pares desfavorecem a lexical por construção: o gerador recusou as perguntas que copiavam cinco palavras seguidas da
seção (`dados-sinteticos.md`).

## Achados

1. **A lexical do desenho original não achava nada.** `websearch_to_tsquery` une os termos com E, e nenhuma seção
   contém todas as palavras de uma pergunta em linguagem natural: recall@3 lexical 0, a híbrida igual à semântica e a
   busca nunca confiável. Com os termos unidos por OU (`plainto_tsquery`, que não lê nenhum operador do usuário), a
   lexical passou a 0,35.
2. **Os erros por seção são mais do rótulo do que da busca.** Lendo os 27 erros da híbrida por seção:
   - **seções genéricas repetidas:** "Prazo" e "Quando uma pessoa analisa" têm quase o mesmo texto em vários
     documentos. "Quem verifica o meu pedido de reembolso de valor mais alto?" está rotulada POL-02, e a busca trouxe
     POL-05, POL-11 e POL-06, que respondem igualmente;
   - **perguntas que não dizem o tipo de incidente** ("Até que horas posso reclamar caso venha algo errado no meu
     pedido?", rotulada qualidade, trouxe item errado e atraso);
   - **rótulo errado:** "veio errado e faltando coisa" está rotulada POL-05 (qualidade);
   - **erro real da busca:** "meu lanche veio todo amassado e a comida estava gelada" não trouxe POL-05; a semântica
     preferiu trechos de prazo.

   Por isso o relatório mostra também o acerto por documento, e a métrica que decide a qualidade da busca no atendimento
   vem dos evals do agente (change 15), com perguntas de clientes simulados.

## Regra de busca não confiável

| Limiar | (a) nas duas listas e similaridade: pares confiáveis / fora recusadas | (b) só similaridade: pares confiáveis / fora recusadas |
| --- | --- | --- |
| 0,80 | 81,7% / 56,7% | 100,0% / 6,7% |
| 0,83 | 81,7% / 70,0% | 100,0% / 23,3% |
| 0,84 | 81,7% / 73,3% | 100,0% / 33,3% |
| **0,85** | 81,7% / 86,7% | **98,3% / 63,3%** |
| 0,86 | 76,7% / 90,0% | 93,3% / 76,7% |
| 0,87 | 65,0% / 93,3% | 76,7% / 93,3% |
| 0,88 | 50,0% / 100,0% | 58,3% / 100,0% |

Maior similaridade semântica de cada consulta: pares de 0,850 a 0,921 (mediana 0,883); fora do escopo de 0,785 a
0,874 (mediana 0,845). As distribuições se sobrepõem entre 0,850 e 0,874.

**Escolha:** regra (b), limiar **0,85**. A regra escrita antes dos números era "o maior limiar que mantém pelo menos
95% dos pares confiáveis". A regra (a), a do desenho original, nunca chega lá: só 81,7% dos pares têm um trecho nas
duas listas. Trocar de regra foi decidido depois de ver os dados, e por isso fica registrado aqui. Como os 60 pares
serviram para escolher o limiar, os 98,3% são otimistas; as perguntas fora do escopo não entraram na escolha e dão a
medida honesta: **63% recusadas**, e 37% passam (por exemplo, "qual a previsão do tempo amanhã").

O limite é aceitável porque a busca não é a única barreira: no agente, uma pergunta fora do escopo cai antes na
intenção `other` do classificador e nem chega à busca.

## Porta no CI

O teste `tests/integration/test_policy_search_quality.py` indexa as políticas num Postgres descartável com o modelo
real e exige, da híbrida com a regra e o limiar padrão:

| Medida | Medido | Piso (medido − 0,05) |
| --- | --- | --- |
| recall@3 por seção | 0,550 | 0,50 |
| MRR@3 por seção | 0,414 | 0,36 |
| Perguntas fora do escopo recusadas | 63,3% | 58% |
| Pares confiáveis | 98,3% | 95% |

## Limites e próximos passos

- 60 pares gerados por LLM a partir das próprias seções: amostra pequena e próxima do texto indexado.
- Seções genéricas repetidas entre documentos limitam qualquer busca por seção; juntar "Prazo" e "Quando uma pessoa
  analisa" num documento comum (POL-11) é uma melhoria de conteúdo, não de busca.
- Não testado: o embedding maior (`bge-m3` ou `Qwen3-Embedding-0.6B`), um reranker e a calibração do limiar com mais
  perguntas fora do escopo.
