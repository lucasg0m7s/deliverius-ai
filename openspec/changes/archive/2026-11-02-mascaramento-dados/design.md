## Context

Motivação em `proposal.md`, requisitos em `specs/mascaramento/spec.md`. O módulo próprio, sem Presidio, já foi decidido
em `docs/seguranca.md`, seção 4, e não é rediscutido aqui.

O estado atual que molda o desenho:

- `observability/logging.py` já tem uma lista plugável de `Redactor` e redige por nome de chave. Faltam o texto livre
  e o texto de exceções.
- `format_exc_info` roda no renderizador, depois da redação. No console, o quadro de exceção é montado também
  depois dela. Reproduzido com um redator de teste: `ValueError: cpf SECRET` saiu sem filtro.
- `observability` é um utilitário e não pode importar módulos de regra. A ligação com `privacy` fica em quem monta a
  aplicação (`api/main.py`, enquanto não existir o `bootstrap`).

Um protótipo descartável, fora do repositório, mediu padrões e normalização com Faker 40.44 pt-BR, 2.000 amostras por
tipo em frases-modelo. Os números abaixo são dele e serão medidos de novo na implementação.

| Tipo | Recall (valor inteiro escondido) |
| --- | --- |
| CPF (com e sem pontuação), CNPJ, cartão, telefone, celular, e-mail, CEP | 100% |
| Endereço com logradouro comum e número | 100% |
| Endereço do Faker com número (inclui "Via", "Trevo", "Viaduto") | 34% |
| Endereço do Faker, todos (inclui os sem número) | 31% |
| Nome próprio | 0% |

Esses 100% são otimistas: frases simples e o formato que o próprio Faker gera. O número que vale para mensagens
realistas vem do eval de privacidade (changes 6 e 15).

## Goals / Non-Goals

**Goals:**

- API pura e pequena, que as changes 12 e 14 usam sem precisar de mudança.
- Falhar para o lado seguro: na dúvida ou no limite, o texto é escondido.
- Limite de recall medido e publicado, com piso protegido por teste.

**Non-Goals:**

- Resistir a quem esconde o próprio dado de propósito ("um dois três", `5 2 9 9 8 2...` com espaço entre cada dígito).
  O risco aqui é o cliente informar o dado por descuido, e o texto é dele.
- Detectar letras parecidas de outro alfabeto (`а` cirílico). O NFKC não as converte, e elas não afetam dados
  numéricos. Fica como risco residual em e-mail e endereço.

## Decisions

### API pública do `privacy`

```python
normalize_text(text) -> str                         # a rede de palavras-chave (change 12) também usa
find_personal_data(normalized) -> tuple[PersonalDataMatch, ...]   # validate_reply (change 12) também usa
redact_text(text) -> str                            # sem estado, marcadores sem número: logger e traces
mask_text(text, ledger, key) -> MaskedText          # conversa, marcadores numerados
MarkerLedger.empty(conversation_id) -> MarkerLedger
```

`redact_text` e `mask_text` se separam porque o logger não tem conversa nem chave. As duas usam o mesmo reconhecimento.

- `MarkerLedger` é um dataclass imutável, com tuplas: serializa no estado do grafo e não tem corrida própria.
- `MaskedText` traz o texto, o novo registro e a contagem por tipo, útil para métrica sem expor valor.
- Tipos em `@dataclass(frozen=True, slots=True)`, como pede `convencoes-codigo.md`.

Módulos: `normalization.py`, `documents.py` (dígitos verificadores e Luhn), `detection.py` (padrões e precedência),
`masking.py` (registro e marcadores).

### Normalização em cinco passos

Remover a categoria `Cf`, aplicar NFKC, converter a categoria `Nd` para `0-9`, converter a categoria `Pd` para `-`,
reduzir espaços horizontais. Quebra de linha fica, para o traceback continuar legível.

- **Por que não só NFKC (o que o roadmap dizia):** medido no protótipo, ele mantém `١٢٣` (árabe-índico) e `–`
  (travessão), que passariam direto por padrões com `[0-9]` e `-`.
- **Padrões usam `[0-9]`, nunca `\d`:** no Python, `\d` casa qualquer dígito Unicode. Com `[0-9]`, chamar o
  reconhecimento com texto não normalizado aparece no teste, em vez de funcionar por acaso.
- **Alternativa descartada:** manter o texto original e mascarar por posição. A normalização muda posições, o mapeamento
  complica, e a saída normalizada é aceitável, porque o original nunca é guardado.

### Reconhecimento em padrões separados, com precedência

Ordem, sem sobreposição: UUID (protegido, nunca mascarado), e-mail, CNPJ, cartão, CPF, oito dígitos soltos (CEP),
telefone, CEP com hífen, endereço. Cada valor reconhecido é apagado do texto de trabalho antes do padrão seguinte, o
que dá o mesmo resultado de reler o texto depois de mascarado e garante a idempotência.

- E-mail vem primeiro para `11987654321@exemplo.com` não virar telefone mais `@exemplo.com`.
- Cartão aceita só dígitos contínuos ou grupos separados, para não engolir `+55 11 9...` (o protótipo mostrou
  telefones viajando como cartão quando o separador era livre).
- Alternativa descartada: um único padrão de "sequência de dígitos", depois classificada. Une dois valores vizinhos
  (`CPF 52998224725 11987654321`) e erra os dois.

**CPF e CNPJ pontuados dispensam o dígito verificador.** `docs/seguranca.md` exige dígito válido, para evitar falso
positivo em números soltos. Mas a pontuação completa quase não aparece por acaso, e um documento com um dígito errado
continua identificando alguém. O dígito segue obrigatório sem pontuação. `seguranca.md` é atualizado.

**CNPJ alfanumérico.** Doze posições `[0-9A-Z]` e dois dígitos. O valor de cada caractere é o ASCII menos 48, com módulo
11 e pesos de 2 a 9 ([perguntas e respostas da Receita](https://www.gov.br/receitafederal/pt-br/centrais-de-conteudo/publicacoes/perguntas-e-respostas/cnpj/cnpj-alfanumerico.pdf)).
Conferido no protótipo: `12ABC34501DE` dá `35`, o valor do exemplo oficial. Aceita minúsculas, porque o cliente digita
assim; o dígito verificador evita a maior parte dos falsos positivos em palavras comuns.

**Lista curta de logradouros.** `rua`, `avenida`, `av`, `travessa`, `alameda`, `estrada`, `rodovia`, `praça`, `largo`,
`ladeira`, `viela`, `vila`, `quadra`, `condomínio`, `residencial`, `loteamento`, `jardim`, com abreviações usuais,
menos `tv`, para `minha tv 42` não virar endereço (`trav.` fica no lugar).

- Alternativa medida: a lista inteira do Faker, com `via`, `vale`, `conjunto`, `parque`. O recall sobre o Faker sobe, mas
  `comprei na via 2 vezes` e `o vale 50 reais` viram endereço, e a frase perde sentido para o LLM. Em uma conversa de
  delivery, essa troca é pior.
- Falso positivo aceito: `rua 12 de outubro` esconde `rua 12`. Custa pouco e pode ser um endereço de verdade.

**Oito dígitos sem separador valem como CEP.** Podem ser um telefone sem DDD. Como o valor some nos dois casos, o tipo é
só informativo.

**Falso positivo aceito em logs:** um número de 10 ou 11 dígitos numa cadeia de texto vira `[TELEFONE]` (ex.:
`1793505923-1aa2`). UUID e carimbo de tempo, que correlacionam os logs, são protegidos. Inteiros não passam pelo filtro.

### Marcadores estáveis sem guardar o valor

A entrada do registro é `(impressão digital, tipo, número)`. A impressão é
`HMAC-SHA256(chave, conversa + tipo + valor canônico)`, e o valor canônico tira a pontuação e ignora maiúsculas.

- **Por que chave secreta:** só existem 10⁹ CPFs. Com hash ou sal guardado junto no mesmo banco, a força bruta leva
  segundos. Sem a chave, o registro não revela nada.
- **Por que a conversa entra na conta:** o mesmo CPF em duas conversas gera impressões diferentes, então o registro não
  serve para ligar conversas.
- **Alternativa descartada:** guardar o valor no estado para comparar. Contraria "o texto original nunca é guardado".
- **Alternativa descartada:** numerar sem identidade, a cada ocorrência. Perde a estabilidade que o roadmap pede.
- **Chave:** `privacy` a recebe como argumento e continua puro. A variável de ambiente entra na change 12, junto com o
  primeiro uso real, com falha na subida se faltar. Trocar a chave só renumera os marcadores de conversas em curso.
- **Honestidade sobre LGPD:** com a chave, um valor conhecido pode ser testado contra o registro. Isso é pseudonimização,
  não anonimização. O registro segue a retenção das mensagens (90 dias).

Um marcador digitado pelo próprio cliente (`[CPF_7]`) não revela nada, porque não existe caminho de volta. Fica como
texto.

### Filtro do logger

Em `observability/logging.py`:

- `format_exc_info` passa para a cadeia comum, **antes** da redação. O console perde o quadro colorido de exceção. É o
  preço de redigir igual nos dois formatos, e o quadro com variáveis locais seria outro vazamento (`show_locals=True`
  é o padrão no structlog 26.1).
- A redação por chave ganha o mesmo limite de 8 níveis: sem ele, uma estrutura circular derrubaria o log antes mesmo
  de chegar ao filtro de texto.
- `text_redactor(mask: Callable[[str], str]) -> Redactor` percorre o evento: `str` passa pela função; `Mapping`, `list`,
  `tuple` são percorridos; `bool`, `int`, `float` e `None` ficam; o resto vira `repr` e passa pela função.
- Mais de 8 níveis vira `REDACTED`. Protege contra estrutura circular, e um log nunca derruba a requisição.
- `api/main.py` liga: `configure_logging(settings, extra_redactors=[text_redactor(redact_text)])`.

**Por que inversão de dependência:** `observability` continua sem importar `privacy`, e o contrato de utilitários fica
de pé.

**Por que inteiros atravessam:** `duration_ms` e `total_cents` são campos legítimos, e um valor de 8 dígitos viraria CEP.
Um CPF logado como inteiro depende da redação por chave (`cpf=`) e da regra de não logar dado pessoal.

### Limites

- **100 valores no registro.** Acima disso, marcador sem número e nada é guardado. Perde estabilidade, nunca privacidade.
- **64.000 caracteres, antes e depois do NFKC.** A API já corta a mensagem em 1.000, então isso protege o logger contra
  um traceback ou um campo gigante. O NFKC pode multiplicar o tamanho por 18 (`U+FDFA`), por isso a segunda conferência.
- `[TEXTO_OMITIDO]` em vez de exceção: uma falha no logger não pode virar erro da requisição.

### Regras de import

- Contrato novo **"privacy is pure"**, com a mesma lista do `policy` e também `deliverius.policy` e `deliverius.orders`.
- Camadas: `policy` e `privacy` lado a lado, sem se importarem (`"deliverius.policy | deliverius.privacy"`; a sintaxe é
  conferida na documentação da versão instalada do import-linter).
- O contrato de utilitários passa a proibir `deliverius.privacy`.

### Testes e medição

- **Conjunto de medição:** `Faker("pt_BR")`, semente fixa, frases-modelo em `tests/support/`. A métrica é o valor inteiro
  estar coberto por um trecho reconhecido, o que importa para privacidade, e não o tipo acertado.
- **Pisos:** 99% para os tipos estruturados, e não 100%, porque o conjunto vai mudar com novas versões do Faker. Nomes e
  endereços raros são medidos sem piso, e o número vai para `seguranca.md`.
- **Faker só no grupo de desenvolvimento.** Fixado pelo `uv.lock`, para a semente dar o mesmo resultado.
- **Propriedades com Hypothesis:** normalizar e mascarar são idempotentes; nunca lançam exceção em texto arbitrário; um CPF
  válido gerado e colocado em texto aleatório nunca aparece na saída.
- **Tempo:** melhor de 3 execuções, para ruído do CI não reprovar. No protótipo, o pior caso foi cerca de 10 ms
  (1.000 × `U+FDFA`).
- **Custo por log:** a implementação registra o tempo do `redact_text` numa mensagem curta, para decidir com número se
  um atalho é necessário.

## Checklist de `docs/robustez.md`, seção 11

| Item | Resposta |
| --- | --- |
| Rota, ferramenta, `AccessScope` | Não se aplica: nenhuma rota, ferramenta ou consulta |
| Corrida | Não se aplica: funções puras e registro imutável. Dois turnos simultâneos da mesma conversa são barrados pela trava de turno da change 13 |
| Idempotência | Mascarar é idempotente, e há teste de propriedade. Sem chave de idempotência, porque não há efeito |
| Timeout e novas tentativas | Não se aplica: nenhuma chamada externa. O tempo é limitado por teste |
| Recursos esgotáveis | Registro com 100 valores, texto com 64.000 caracteres e 8 níveis no log; ao estourar, ver "Limites" |
| Queda no meio | Não se aplica: sem estado fora do retorno. O registro novo só existe se a função terminar |
| Transação com rede ou LLM | Não se aplica |
| Eventos de log | Nenhum evento novo: `privacy` é puro. A contagem por tipo volta em `MaskedText`, e a change 12 decide o evento e a métrica |
| Texto de fora e regex | Sim, é o assunto da change: normalização antes de tudo, padrões com repetição limitada, sem quantificador aninhado, testados com entradas hostis |
| Grafo, banco, API | Nenhuma mudança agora. A saída do console muda só no formato da exceção. `MarkerLedger` entra no estado de forma aditiva na change 12 |
| Consultas e índices | Não se aplica |
| Travas e migrations | Não se aplica |
| Configuração nova | Nenhuma nesta change. A chave entra na change 12, com falha na subida |

## Avaliação

Não há LLM nem modelo. O mascaramento é avaliado pelo recall por tipo no conjunto do Faker, com piso de 99% nos tipos
estruturados, e, mais tarde, pelo eval de privacidade sobre mensagens rotuladas (change 15).

## Risks / Trade-offs

- [Nomes próprios passam, com recall de 0%] → Limite publicado, com o número. O nome do cliente no texto chega ao LLM,
  mas sem documento nem contato. O eval de privacidade decide se o NER em português entra, com o custo registrado.
- [Endereço informal ou sem número passa] → Mesmo tratamento: medido e publicado. As ferramentas buscam o endereço pelo
  pedido, então o cliente raramente precisa escrevê-lo.
- [100% do Faker dá falsa confiança] → O design e `seguranca.md` deixam claro o que esse número mede e citam o eval
  realista como referência.
- [Falso positivo tira contexto do LLM ou de um log] → Conjunto negativo em teste, com os casos comuns de pedido. Os
  aceitos estão escritos neste design.
- [Regex no caminho de todo log] → Custo medido. Limite de tamanho protege contra o pior caso.
- [Console sem o quadro de exceção atrapalha o desenvolvimento] → Texto simples mantém o traceback completo. Segurança
  igual nos dois formatos vale mais.
- [Traceback redigido esconde um dado útil para depuração] → Aceito: é exatamente a regra do projeto.
- [Falta de `timeout` no módulo `re`] → Padrões sem retrocesso ruim e teste de tempo. A biblioteca `regex`, com timeout,
  seria uma dependência a mais em produção, e o protótipo mostra que ela não é necessária.

## Migration Plan

Sem migração: módulo novo, sem dados. O filtro entra ao subir a nova versão, e reverter é desfazer a linha em
`api/main.py`.
