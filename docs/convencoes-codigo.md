# Convenções de código — Deliverius AI

Tema 6 da etapa de arquitetura, decidido em 2026-10-31. As bases são a [PEP 8](https://peps.python.org/pep-0008/),
as regras do Ruff e do mypy, e as decisões de `arquitetura.md`. O que as ferramentas conseguem checar está em
`pyproject.toml`; este documento cobre o resto.

Quando uma regra não servir num caso concreto, a exceção é comentada no código com o motivo.

## Versão e ferramentas

| Ferramenta | Decisão | Por quê |
| --- | --- | --- |
| **Python** | **3.13** (`requires-python = ">=3.13,<3.14"`), instalado pelo `uv` | Está estável há um ano e todas as bibliotecas do projeto publicam pacotes para ele. O 3.14 é mais novo, mas não traz nada de que precisemos, e muda a avaliação de anotações (PEP 649), o que ainda pega bibliotecas como Pydantic e SQLAlchemy em casos raros. Se algum pacote faltar no `uv sync`, voltamos para o 3.12 e registramos aqui |
| Projeto e dependências | `uv` com `uv.lock` versionado; grupos `dev` e `research` | Um comando instala tudo igual em qualquer máquina. No Kaggle e no Colab, o `uv` baixa o próprio Python, então a versão do ambiente não importa |
| Formatação e lint | **Ruff** (`ruff format` e `ruff check`) | Uma ferramenta só, rápida, que substitui black, isort, flake8 e bandit |
| Tipos | **mypy `--strict`** com o plugin do Pydantic | Estável e com suporte a Pydantic e SQLAlchemy. O `ty`, da Astral, ainda está em beta e sem suporte a Pydantic garantido; será reavaliado depois do 1.0 |
| Fronteiras | **import-linter** | As regras de import de `arquitetura.md` quebram o build |
| Testes | pytest, pytest-asyncio, **Hypothesis**, **Testcontainers** (Postgres com pgvector), pytest-cov | Ver "Testes" |
| Logs | **structlog**: JSON em produção, legível no desenvolvimento | Logs estruturados com o ID do trace (tema 9) |
| Ganchos locais | pre-commit com Ruff e Gitleaks | Pega formatação e segredo antes do commit; o CI repete tudo |

**Regras do Ruff** além das que já estão no rascunho: `PT` (pytest), `TRY` (exceções), `PERF`, `C4`, `FURB`, `PL`
(pylint, sem as regras de "número mágico" e de "argumentos demais"), **`DTZ`** (proíbe `datetime` sem fuso) e
**`T20`** (proíbe `print`).

## Idioma

A regra: **quem lê decide o idioma.** O que desenvolvedores e ferramentas leem fica em inglês; o que se escreve
para documentar o projeto fica em português; o que o cliente final lê fica em português, porque é produto.

| Inglês | Português |
| --- | --- |
| Código: identificadores, comentários, docstrings, nomes de teste, nomes de arquivo | Documentação em `docs/`, README, specs e changes do OpenSpec |
| Configuração: chaves e comentários do `pyproject.toml`, `.env.example`, `docker-compose.yml`, workflows e Dockerfile | Mensagens de commit e de PR (Conventional Commits, descrição em português) |
| Mensagens de log e de exceção | Prompts (`agent/prompts/`, `evals/prompts/`), porque instruem um modelo que responde em português |
| Erros da API: `code` estável (`approval_already_decided`), `title` e `detail` dos *problem details* | O que o cliente final lê: respostas do Deli, textos fixos do modo degradado e da transferência, textos da interface |
| Nomes de métricas, spans, atributos e eventos | Nomes do domínio na documentação, com o nome em inglês do código ao lado (`glossario.md`) |

**Por que os erros da API ficam em inglês:** é o padrão das APIs públicas (Stripe, GitHub, Google). A API é lida
por quem a integra, e o `code` estável é o contrato: a interface o traduz numa mensagem em português para o cliente,
que a API nunca monta. Assim, a mesma API serve qualquer interface, o log e o erro mostram o mesmo texto, e
traduzir não quebra o contrato. O RFC 9457 permite `title` e `detail` localizados pelo `Content-Language`; isso só
entra se um dia houver integrador que não leia inglês.

**Quando o cliente vê um erro**, como "pedido não encontrado" no chat, quem escreve o texto é a interface ou o
`compose_reply`, a partir do `code`, nunca o texto da exceção.

## Python

- **Nomes:**
  - classes em `PascalCase`; funções, variáveis e módulos em `snake_case`; constantes em `UPPER_SNAKE_CASE`;
  - nomes do domínio em inglês, seguindo `glossario.md`;
  - **"agent" é só o agente de IA.** A pessoa que aprova é `reviewer`, e o atendimento humano é `human_support`.
- **Tipos em toda função**, inclusive nas privadas.
  - Nada de `Any`, exceto na fronteira com JSON externo, e mesmo lá ele vira modelo na primeira linha.
  - Use `X | None`, `list[int]` e `type Alias = ...`.
- **Imutável por padrão:** `@dataclass(frozen=True, slots=True)`, `tuple` em vez de `list` quando não muda, e
  `Final` em constantes.
- **Variantes com `match` exaustivo.** Resultados e estados com variantes, como `PolicyDecision` (permitida,
  precisa de aprovação, negada), são uniões de dataclasses tratadas com `match` mais `assert_never`. Assim o mypy
  acusa o caso esquecido.
- **Dinheiro só como `Money`** e tempo só com fuso (`datetime` aware, por `Clock`). O Ruff `DTZ` barra
  `datetime.now()` sem fuso.
- **Sem `print`.** Logs passam pelo structlog e **nunca** levam dado pessoal, texto não mascarado, prompt completo
  nem segredo. Um filtro de mascaramento no logger é a segunda barreira, não a primeira.
- **Comentário só para o porquê não óbvio:** uma restrição externa, um contorno de bug, um valor que parece errado
  mas é de propósito. Docstring curta (uma linha sempre que der) só em API pública que o nome e a assinatura não
  explicam. É a mesma regra do Fuja.
- **Imports absolutos** (`from deliverius.policy import evaluate`) e sempre da API pública do módulo, o
  `__init__.py`, nunca de arquivo interno de outro módulo.

## Código limpo e SOLID

O código tem de se explicar sozinho: nomes claros, funções pequenas, responsabilidades bem definidas. Comentário é
exceção (ver "Python"). Os princípios SOLID valem **na medida em que se pagam**, sem a cerimônia de Java: aqui eles
aparecem como módulos por capacidade, portas leves e injeção pelo `bootstrap` (`arquitetura.md`).

| Princípio | Como aparece no projeto |
| --- | --- |
| **S**: responsabilidade única | Cada módulo tem uma capacidade (`policy` decide, `actions` executa, `approvals` pausa) e cada função faz uma coisa. Se o nome precisa de "e" (`validate_and_execute`), são duas funções |
| **O**: aberto para extensão | Variantes novas entram como um caso a mais numa união com `match` exaustivo, ou como um adaptador novo atrás de uma porta (outro provedor de LLM), sem mexer em quem usa |
| **L**: substituição de Liskov | Todo adaptador cumpre o contrato inteiro da porta, inclusive os falsos dos testes: mesmo tipo de retorno, mesmos erros, nenhum caso "não implementado" |
| **I**: interfaces pequenas | Um `Protocol` declara só o que o chamador usa (`IntentClassifier` tem `classify`, não dez métodos). Melhor duas portas pequenas que uma grande |
| **D**: inversão de dependência | A regra de negócio depende de portas, não de bibliotecas; quem escolhe a implementação é o `bootstrap`. Mas porta só existe onde há troca real ou falso de teste: nada de interface "por precaução" |

**Regras práticas:**

- **Funções curtas e de um nível de abstração.** Se uma função mistura regra de negócio com detalhe de SQL ou de
  HTTP, separe. Uma função passa de cerca de 30 linhas ou de três níveis de indentação → quase sempre são duas.
- **Nomes que dizem a intenção:** `refund_amount_cents`, não `val`; `is_within_cancellation_window`, não `check`.
  Booleanos começam com `is_`, `has_` ou `can_`. Sem abreviação que não esteja no `glossario.md`.
- **Retorno antecipado** em vez de `if` aninhado; cláusulas de guarda no início.
- **Sem código morto nem comentado.** O git guarda o histórico.
- **Sem duplicação de regra.** Um número da política, uma feature do antifraude ou uma checagem de autorização vive
  num lugar só. Duplicar código simples é aceitável; duplicar regra não.
- **Simples antes de genérico (KISS, YAGNI).** Nada de classe base, fábrica ou parâmetro para um caso que ainda não
  existe. Abstrair quando o segundo caso real aparecer.
- **Efeito colateral explícito.** Funções que só calculam não gravam, não chamam rede e não leem o relógio; o que
  tem efeito recebe as dependências por parâmetro.
- **Docstring só onde o nome e a assinatura não bastam,** em uma linha sempre que der. Comentários explicam o porquê
  não óbvio, nunca o quê.

## Modelos de dados: o que usar onde

| Onde | Tipo | Por quê |
| --- | --- | --- |
| Domínio (`orders`, `policy`, `actions`, `approvals`, `privacy`) | `@dataclass(frozen=True, slots=True)` | Puro, leve e sem validação repetida dentro do sistema |
| Fronteiras: API, saída estruturada do LLM (`ActionProposal`), argumentos de ferramenta, configuração, `manifest.json` dos modelos | **Pydantic v2** | É onde entra dado de fora e é preciso validar e gerar JSON Schema |
| Estado do LangGraph | `TypedDict`, com modelos Pydantic dentro | Formato recomendado pelo LangGraph; os valores aninhados são serializáveis pelo checkpointer |
| Banco | Classes mapeadas do SQLAlchemy 2 (`Mapped[...]`) em `db/` | Nunca saem do repositório: o repositório converte para o modelo do domínio |

**Regra:** valide uma vez, na entrada; dentro do sistema, confie no tipo.

## Assíncrono

- **Entrada e saída são `async`:** API, banco, LLM e HTTP.
- **Nada bloqueante dentro de `async`.** O Ruff `ASYNC` ajuda a pegar isso. Inferência de modelo e cálculo pesado
  rodam com `await asyncio.to_thread(...)`.
- **Toda chamada externa tem timeout.**
- **Tarefas concorrentes com `asyncio.TaskGroup`,** nunca `create_task` solto: um erro numa tarefa esquecida some
  sem ninguém ver.
- **Uma sessão do banco por requisição** ou por execução do grafo, aberta pelo chamador e nunca guardada em
  variável global.

## Erros

- **O que é esperado é resultado, não exceção.** A política negar um reembolso faz parte do negócio, então é um
  `PolicyDecision` do tipo negada. Exceção é para o que não deveria acontecer.
- **Exceções do projeto:**
  - todas herdam de `DeliveriusError`;
  - `NotFoundError`, `ConflictError` (idempotência, trava otimista) e `ExternalServiceError` (LLM, Hugging Face)
    têm significado claro.
- **A API traduz exceções para *problem details* (RFC 9457),** como no Fuja, com um código de erro estável. O
  detalhe interno nunca vai para o cliente.
- **Nunca `except Exception: pass`.** Só se captura o que se sabe tratar. O resto sobe, é registrado e vira uma
  resposta 500 genérica.
- **Falha de LLM nunca vira ação.** No pior caso, a conversa vai para o modo degradado (tema 3).

## Configuração

- **Uma classe `Settings` (pydantic-settings)** com subgrupos (`db`, `llm`, `models`, `policy`), lida uma vez no
  `bootstrap` e passada adiante. Nada de `os.environ` espalhado.
- **Segredos como `SecretStr`.** Variável nova entra no `.env.example` no mesmo commit.

## Testes

Mesma regra do Fuja: **cada comportamento é testado numa camada só,** a mais barata que o alcança.

| Camada | O que cobre | Ferramentas |
| --- | --- | --- |
| **Unitário** (padrão do `pytest`) | Política, mascaramento, `Money`, features do antifraude, regra de decisão, validação da resposta, roteamento, cada nó do grafo isolado | pytest; **Hypothesis** para propriedades, como "nenhum CPF válido sobrevive ao mascaramento" e "soma de `Money` nunca perde centavo" |
| **Grafo de ponta a ponta com LLM falso** | Os cenários de `fluxos-do-agente.md`, com o modelo falso roteirizado (`llm/fake.py`) e repositórios em memória | pytest; roda em todo PR, sem rede |
| **Integração** (`-m integration`) | Repositórios, migrations, busca híbrida, checkpointer, idempotência sob concorrência, trigger da auditoria | **Testcontainers** com `pgvector/pgvector:pg17`: mesmo banco no local e no CI, cada execução limpa |
| **LLM real** (`-m llm`) | Poucas chamadas que confirmam que a integração com cada provedor funciona | Fora do padrão; rodam sob demanda |
| **Evals** | Qualidade do agente | Não são testes do pytest; ficam no tema 7 |

- **Cobertura:**
  - **100% dos ramos** em `policy`, `privacy`, `actions` e `fraud/features`, onde um erro custa dinheiro ou expõe
    dado;
  - **85%** no total;
  - o CI barra abaixo disso.
- **O nome do teste descreve o comportamento,** em inglês, porque é código:
  `test_missing_item_refund_is_capped_at_item_price`.
- **Arrange / Act / Assert,** um comportamento por teste.
- **Sem rede nem relógio real nos testes unitários.** O tempo vem do `Clock` falso e o LLM do falso.

## Dependências

Antes de adicionar um pacote:

- ele precisa resolver algo que a biblioteca padrão ou um pacote já adotado não resolve;
- precisa ter sido mantido nos últimos 12 meses e ter uso amplo;
- a licença precisa ser MIT, BSD ou Apache;
- ele vai para o grupo certo: principal (aplicação), `dev` ou `research`. **Torch, Transformers e MLflow nunca vão
  para o principal** (tema 1);
- ele é registrado em `arquitetura.md` com o motivo, no mesmo commit (`build(deps): adiciona <pacote> para
  <motivo>`).

## Commits

Seguem o [Conventional Commits 1.0.0](https://www.conventionalcommits.org/pt-br/v1.0.0/), com a descrição em
português. O gancho `conventional-pre-commit` (estágio `commit-msg` do pre-commit) recusa a mensagem fora do
formato.

```
<tipo>(<escopo>)!: <descrição>

<corpo: o porquê da mudança, não o quê>

<rodapé: BREAKING CHANGE: ..., Refs: <change do OpenSpec>>
```

- **Tipos** (palavras-chave da especificação, em inglês): `feat` (funcionalidade nova, gera versão *minor*),
  `fix` (correção, gera *patch*), `docs`, `test`, `refactor` (sem mudar comportamento), `perf`, `build`
  (dependências, Docker), `ci`, `chore`, `style` (formatação), `revert`.
- **Escopo** é o módulo ou a área: `policy`, `privacy`, `actions`, `approvals`, `agent`, `api`, `db`, `llm`,
  `fraud`, `intent`, `datagen`, `evals`, `deps`, `openspec`. Opcional quando a mudança é transversal.
- **Descrição:** em português, minúscula, no presente da 3ª pessoa (completa a frase "este commit…": `adiciona`,
  `corrige`, `remove`), sem ponto final, com até 72 caracteres.
  - Bom: `feat(policy): adiciona teto de reembolso por pedido`
  - Ruim: `feat: Adicionei coisas.`, `fix: ajustes`, `wip`
- **Mudança que quebra compatibilidade:** `!` depois do tipo ou do escopo **e** o rodapé `BREAKING CHANGE:` (o
  token fica em inglês e em maiúsculas, como exige a especificação) explicando o que muda para quem usa.
- **Rodapé `Refs:`** com o nome da change do OpenSpec (`Refs: nucleo-dominio`).
- **Um commit, uma mudança lógica,** e cada commit passa na verificação do `CLAUDE.md`. Refatoração oportunista
  vai num commit `refactor` separado, nunca misturada com `feat` ou `fix`.
- **Branch e PR:** uma change do OpenSpec por branch (`feat/nucleo-dominio`); o título do PR segue o mesmo formato
  do commit. O CHANGELOG sai dos commits (`entrega.md`).

## Disciplina de mudança (o que costuma ser esquecido)

Regras para quem escreve código aqui, pessoa ou agente de IA. São os erros mais comuns de quem gera código
depressa.

- **Nunca afrouxar a verificação para passar.** Nada de baixar a cobertura mínima, marcar teste com `skip` ou
  `xfail`, apagar ou enfraquecer uma asserção, ou acrescentar `# noqa` ou `# type: ignore` para silenciar um erro.
  Se a regra estiver errada, muda-se a regra num commit próprio, com motivo, e o Lucas decide.
- **Bug corrigido vem com o teste que falhava antes da correção.**
- **O caminho triste é testado:** entrada vazia, exatamente no limite e um a mais, Unicode estranho, dependência
  falhando, objeto de outro dono, operação repetida. Só o caminho feliz testado não é teste.
- **A mudança fica completa no mesmo commit ou change:** migration, `.env.example`, matriz de autorização,
  `glossario.md`, documentação e README acompanham o código que os torna necessários.
- **API de biblioteca conferida na documentação da versão instalada** (no `uv.lock`), não de memória. Função,
  parâmetro ou comportamento inventado é o erro mais caro de detectar.
- **Nada fora do escopo pedido.** Melhorias vistas no caminho viram uma nota no roadmap ou uma proposta, não uma
  alteração silenciosa.
- **Sem `TODO` solto.** Ou vira tarefa no `tasks.md` ou no roadmap, ou não entra.
- **Trabalho caro uma vez só:** carregar modelo, compilar regex, montar cliente HTTP ou de banco acontece no
  `bootstrap`, nunca por requisição.
- **Recurso aberto é recurso fechado:** sessão, conexão, arquivo e cliente HTTP com `async with`, ou fechados no
  `lifespan`.
- **Nível de log certo:** `info` para decisões do sistema (`policy.denied`), `warning` para algo que se recuperou
  sozinho (reserva assumiu), `error` só para o que precisa de alguém olhar. Nada de log em laço por item.

## O que evitar

| Evite | Por quê e o que fazer |
| --- | --- |
| `float` para dinheiro | Erra centavos; use `Money` |
| `datetime.now()` sem fuso, ou `time.time()` na regra | Use `Clock`; senão os prazos não podem ser testados |
| `dict` solto cruzando módulos | Use um dataclass ou um modelo Pydantic |
| Pydantic dentro do domínio, ou dataclass na fronteira | Ver "Modelos de dados" |
| Importar arquivo interno de outro módulo | Use o `__init__.py`; o import-linter barra as camadas |
| Ferramenta do agente que escreve | O agente só lê e propõe (tema 2) |
| ID de cliente ou pedido vindo de argumento do LLM | Vem do contexto de execução (tema 2) |
| Texto do cliente sem mascarar em log, prompt, banco ou trace | Vazamento de dado pessoal (LGPD) |
| Prompt como string no código | Arquivo versionado em `prompts/` (tema 3) |
| Chamada bloqueante em `async`, ou `create_task` solto | Use `to_thread` e `TaskGroup` |
| `except Exception: pass`, ou `# type: ignore` sem motivo | Esconde bug; trate o erro ou explique na mesma linha |
| Variável global com estado (cliente de banco, modelo, provedor) | Monte no `bootstrap` e injete |
| Comentário que repete o código | Melhore o nome; comentário só para o porquê |
| Notebook com lógica de treino | Script em `research/`; o notebook só chama e mostra |
| Função longa que mistura regra, banco e HTTP | Separe por responsabilidade; ver "Código limpo e SOLID" |
| Interface, classe base ou fábrica sem segundo caso real | Abstraia quando o segundo caso aparecer |
| Código comentado ou morto | Apague; o git guarda o histórico |
| Teste pulado, asserção afrouxada ou cobertura baixada para o CI passar | Corrija o código; ver "Disciplina de mudança" |
| Carga preguiçosa de relacionamento no SQLAlchemy assíncrono | `lazy="raise"` e carga explícita; ver `robustez.md`, seção 8 |
| Mensagem de commit fora do Conventional Commits, ou commit que mistura assuntos | Ver "Commits" |
