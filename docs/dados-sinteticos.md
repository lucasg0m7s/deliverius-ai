# Relatório dos dados sintéticos (v1.0.0)

Resultado da change `gerador-dados`. O desenho está em `modelo-dominio.md`, seção 4, e as decisões em
`decisoes-arquitetura.md`. Versão do gerador 1.0.0, semente 42, instante de referência 2026-10-31T23:59:59Z.

## Conjuntos

| Conjunto | Tamanho | Divisão |
| --- | --- | --- |
| Mensagens rotuladas (Gemini Flash-Lite) | 2.969 em 297 cenários, 11 intenções (269 a 270 cada) | Treino 2.089, validação 440, teste 440, por cenário |
| Teste de outro gerador (Groq `gpt-oss-120b`) | 550 em 55 cenários (50 por intenção) | Só teste; cenários disjuntos dos do conjunto principal |
| Mundo simulado | 2.000 clientes, 60 restaurantes, 20.000 pedidos em seis meses | — |
| Histórico do antifraude | 3.378 reclamações, 11,8% com rótulo de fraude, 2,49% de rótulos trocados de propósito | Treino 2.880 (até 30/09), teste 498 (outubro) |
| Pares pergunta → seção da política | 60, de 48 seções dos 12 documentos | — |
| Cenários de ponta a ponta | 150: F1 30, F2 25, F3 20, F4 25, F5 20, F6 15, F7 15 | — |

## Mensagens

- **Dados pessoais:** 890 mensagens (30%) trazem pelo menos um dado pessoal falso, com a posição gravada: CPF 338,
  e-mail 310, nome 280, telefone 220, endereço 210.
- **Prompt injection:** 150 mensagens (5%) trazem um ataque do catálogo de 30, nunca na intenção `sensitive`.
- **Tamanho:** mediana de 212 caracteres, p95 de 396.
- **Vocabulário distinto por intenção:** de 1.375 palavras (`item_missing`) a 2.285 (`sensitive`).
- **Descartes:** 91 variações (3,0%), todas por marcador de dado pessoal que o cenário não pediu. Pior intenção:
  `not_delivered`, com 30 descartes em 300 variações (10%), abaixo do limite de 20%. Os descartes vêm em blocos de
  10: quando o modelo inclui um marcador a mais, costuma fazer isso nas dez variações do cenário, que é então
  repetido na tentativa seguinte. Nenhum cenário falhou nas três tentativas.

## Revisão à mão

100 mensagens sorteadas do conjunto principal (9 ou 10 por intenção, semente 2026), lidas uma a uma, contando as que
não expressam a intenção do rótulo.

**Defeitos de rótulo: 5 de 100 (5%)**, abaixo do limite de 10%.

| Rótulo | O que a mensagem diz | Rótulo mais adequado |
| --- | --- | --- |
| `billing_issue` | Cobrança errada, e "tô passando mal" | Ambígua: o mal-estar puxa para `sensitive` |
| `late_delivery` | O status mudou para entregue sem a pessoa receber | `not_delivered` |
| `policy_question` | Abre reclamando da qualidade da comida e pergunta quanto recebe de volta | Ambígua com `quality_issue` |
| `policy_question` | O pedido veio incorreto; quanto tem direito de receber | Ambígua com `wrong_item` |
| `sensitive` | Comida com cheiro de estragada, sem relato de mal-estar | `quality_issue` |

**Limites de naturalidade** (não erram o rótulo, mas um classificador pode aprender o atalho):

- dados pessoais às vezes aparecem colados no fim da frase, de um jeito que pouca gente escreve ("dados Fulana e
  e-mail");
- o ataque de prompt injection vem quase sempre na última frase da mensagem;
- personas com gíria repetem "mano" mais do que o natural;
- variações de um mesmo cenário às vezes repetem a mesma estrutura com outras palavras.

Por isso o número que decide o classificador é o do teste escrito por pessoas (`ml-e-avaliacao.md`, seção 1.3), e o
teste de outro gerador mede quanto o modelo decorou o estilo do Gemini.

## Reprodutibilidade

- As camadas sem LLM (mundo, reclamações, resolução, cenários de ponta a ponta e divisões) dependem só da semente; um
  teste de hash dourado fixa a saída de um mundo pequeno.
- As mensagens dependem do arquivo de cache de respostas e do registro de tentativas no mesmo arquivo. Repetir a
  geração com o mesmo cache não faz chamada real e produz arquivos idênticos byte a byte, inclusive o manifesto.
- Quatro defeitos apareceram só na geração real e foram corrigidos com teste de regressão: a tentativa que falhava
  não ia para o cache (a repetição mudava o texto); uma pausa curta do Groq, pelo limite de 8.000 tokens por minuto,
  encerrava a geração; a resposta estruturada do cliente compatível com a OpenAI não era relida do cache; e, com
  cache, o orçamento só contava e não barrava, o que agora o próprio gerador confere antes de cada tentativa.

## Custo

A geração completa usou cerca de 350 chamadas ao Gemini (cerca de 370 mil tokens) e 55 ao Groq, dentro da cota
gratuita de um dia de cada provedor.
