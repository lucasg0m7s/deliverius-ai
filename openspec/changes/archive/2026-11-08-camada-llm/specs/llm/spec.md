## Purpose

Dar a toda chamada de LLM do sistema um papel, um provedor trocável, uma reserva, um teto diário e um registro de uso e
custo, de modo que a falha ou o fim da cota de um provedor nunca vire erro para o cliente nem uma ação.

## ADDED Requirements

### Requirement: Papéis configuráveis por ambiente

O sistema SHALL resolver todo modelo a partir de um papel (`agent`, `writer`, `customer_sim`, `judge`, `datagen`). Cada
papel MUST ter modelo principal configurável por variável de ambiente, reserva opcional, timeout e teto de tokens de
saída. A montagem MUST falhar, com uma mensagem que cite a variável, quando faltar a chave do provedor de um papel
configurado ou quando a referência do modelo for inválida. O valor da chave MUST NOT aparecer nessa mensagem.

#### Scenario: Trocar o modelo de um papel

- **WHEN** a variável do papel `writer` aponta para outro provedor e modelo
- **THEN** o papel passa a usar esse modelo, sem mudança no código de quem chama

#### Scenario: Chave faltando

- **WHEN** um papel usa o Groq e a variável da chave do Groq não existe
- **THEN** a montagem falha citando `GROQ_API_KEY`

#### Scenario: Provedor desconhecido

- **WHEN** a referência do modelo cita um provedor que não existe
- **THEN** a configuração é recusada com uma mensagem que cita a variável

### Requirement: Três integrações de provedor

O sistema SHALL criar modelos para Gemini, para servidores compatíveis com a OpenAI (Groq e Ollama) e para Anthropic,
todos atrás do mesmo contrato de modelo de chat. Os destinos de rede MUST vir só da configuração. As novas tentativas
internas dos SDKs MUST ficar desligadas.

#### Scenario: Cada provedor na fábrica

- **WHEN** um modelo é criado para cada um dos três tipos de integração
- **THEN** cada um tem o modelo, o timeout e o teto de saída do papel, e nenhuma nova tentativa interna

#### Scenario: Um pedido por tentativa

- **WHEN** um servidor compatível com a OpenAI responde `500`
- **THEN** o SDK faz um único pedido por tentativa

### Requirement: Novas tentativas e reserva

O sistema SHALL repetir uma chamada só em erro passageiro (limite de requisições, erro do servidor, conexão, timeout), no
máximo 2 vezes, com espera exponencial e jitter. Esgotadas as tentativas, ou com erro não passageiro, a chamada MUST ir
para a reserva do papel, se houver. Toda tentativa MUST contar no orçamento e no registro de uso.

#### Scenario: Erro passageiro

- **WHEN** o modelo falha uma vez com erro de servidor e depois responde
- **THEN** a resposta é devolvida, e duas tentativas ficam registradas

#### Scenario: Erro que não adianta repetir

- **WHEN** o provedor recusa a chave ou o pedido
- **THEN** não há nova tentativa no mesmo provedor, e a chamada vai para a reserva

#### Scenario: Principal fora do ar

- **WHEN** o principal esgota as tentativas
- **THEN** a reserva responde, e o log registra a troca

### Requirement: Modo degradado sem ação

O sistema SHALL oferecer uma chamada que devolve, como resultado tipado, a resposta do modelo ou o modo degradado. Falha
de todos os provedores, orçamento esgotado, banco do orçamento fora do ar ou prazo total estourado MUST resultar no
modo degradado, sem lançar exceção de provedor ao chamador. O cancelamento da tarefa MUST continuar se propagando.

#### Scenario: Todos os provedores falham

- **WHEN** o principal e a reserva falham
- **THEN** o resultado é o modo degradado, com o motivo, e o evento `llm.degraded` é registrado

#### Scenario: Modelo travado

- **WHEN** o modelo não responde dentro do prazo total do papel
- **THEN** o resultado é o modo degradado

#### Scenario: Tarefa cancelada

- **WHEN** a tarefa que faz a chamada é cancelada
- **THEN** o cancelamento se propaga, em vez de virar modo degradado

### Requirement: Orçamento diário com reserva atômica

O sistema SHALL reservar uma requisição no banco antes de cada chamada, por provedor e por dia em UTC, recusando-a quando
o limite de requisições ou de tokens do dia foi atingido. A reserva e a conferência MUST ser uma única operação atômica, e
uma reserva nunca é devolvida. Se o banco estiver fora do ar, a chamada MUST ser recusada. Ao cruzar 80% do limite, o
sistema MUST registrar um aviso uma vez. A única exceção é o modelo com cache, descrita em "Cache só para evals e geração
de dados".

#### Scenario: Chamadas disparadas juntas

- **WHEN** 50 chamadas são disparadas juntas com 10 requisições restantes
- **THEN** exatamente 10 são reservadas e 40 recusadas, e o contador termina no limite

#### Scenario: Orçamento esgotado no principal

- **WHEN** o principal não tem mais orçamento e a reserva tem
- **THEN** o principal não é chamado, e a reserva responde

#### Scenario: Orçamento esgotado em todos

- **WHEN** nenhum provedor do papel tem orçamento
- **THEN** nenhum modelo é chamado, e o resultado é o modo degradado

#### Scenario: Limite de tokens

- **WHEN** os tokens do dia atingiram o limite
- **THEN** a próxima reserva é recusada, mesmo com requisições sobrando

#### Scenario: Virada do dia

- **WHEN** o dia em UTC muda
- **THEN** o contador recomeça

#### Scenario: Provedor sem crédito

- **WHEN** o limite de um provedor é zero, o padrão da Anthropic
- **THEN** nenhuma chamada real é feita a ele

### Requirement: Limite de requisições e respeito ao Retry-After

O sistema SHALL limitar as requisições por minuto de cada provedor, com um limitador compartilhado por todos os papéis
desse provedor. Depois de um `429`, o sistema MUST pausar o provedor pelo tempo pedido na resposta, ou por 5 segundos se
a resposta não disser, com teto de 1 hora. Com uma pausa de até 2 segundos restantes, a chamada espera; com uma maior,
ela é recusada na hora, sem sair para a rede.

#### Scenario: 429 com espera longa

- **WHEN** o principal responde `429` pedindo 60 segundos
- **THEN** a chamada vai para a reserva, e nenhuma outra sai para o principal durante esses 60 segundos

#### Scenario: Espera curta

- **WHEN** faltam até 2 segundos de pausa
- **THEN** a chamada espera esse tempo e sai

#### Scenario: Espera ilegível

- **WHEN** o `429` não traz um tempo que se possa ler
- **THEN** a pausa é de 5 segundos

#### Scenario: Limitador compartilhado

- **WHEN** dois papéis usam o mesmo provedor
- **THEN** os dois dividem o mesmo limite de requisições por minuto

#### Scenario: Rajada de um turno

- **WHEN** o provedor tem um limite de 10 requisições por minuto e o sistema acabou de subir
- **THEN** as 5 primeiras chamadas saem sem esperar, e nenhum minuto passa de 10 requisições

### Requirement: Registro de uso e custo

O sistema SHALL gravar uma linha em `llm_calls` por tentativa real, com sucesso ou erro: papel, provedor, modelo, versão
e hash do prompt, tokens de entrada, de saída e de raciocínio, latência, custo real, custo equivalente, situação e tipo
do erro. A linha MUST NOT conter texto de mensagem nem texto de erro do provedor. Uma falha ao gravar o registro MUST
NOT derrubar a resposta já obtida. Um modelo sem preço conhecido MUST ficar com o custo equivalente vazio, e nunca zero.

#### Scenario: Chamada com sucesso

- **WHEN** o modelo responde com os tokens informados
- **THEN** a linha tem os tokens, a latência, a versão do prompt e o custo calculado pela tabela de preços

#### Scenario: Cota gratuita

- **WHEN** o provedor não está configurado como pago
- **THEN** o custo real é zero, e o custo equivalente segue o preço de lista

#### Scenario: Provedor não informa os tokens

- **WHEN** a resposta vem sem contagem de tokens
- **THEN** os tokens e o custo equivalente ficam vazios, e a resposta é devolvida

#### Scenario: Banco cai depois da resposta

- **WHEN** a gravação do registro falha
- **THEN** a resposta é devolvida, e o evento `llm.usage_record_failed` é registrado

#### Scenario: Conteúdo fora do registro

- **WHEN** uma chamada com um texto conhecido falha ou tem sucesso
- **THEN** esse texto não aparece em `llm_calls` nem nos logs

### Requirement: Prompts versionados

O sistema SHALL carregar prompts de arquivos com cabeçalho de versão, papel e o que mudou, e devolver texto, versão e
hash do conteúdo. Um arquivo sem cabeçalho válido MUST ser recusado na carga, e pedir uma versão que não existe MUST ser
um erro. O cabeçalho MUST NOT ser enviado ao modelo.

#### Scenario: Versão e hash registrados

- **WHEN** uma chamada usa um prompt carregado
- **THEN** a linha de `llm_calls` tem a versão e o hash desse prompt

#### Scenario: Cabeçalho inválido

- **WHEN** o arquivo não tem a versão no cabeçalho
- **THEN** a carga falha citando o arquivo

### Requirement: Cache só para evals e geração de dados

O sistema SHALL oferecer um cache de respostas em arquivo local cuja chave inclui prompt, modelo, parâmetros, ferramentas
e número da tentativa. O cache MUST ser desligado por padrão e só existir quando pedido explicitamente pelo chamador; os
modelos sem cache MUST NOT usar um cache global. Um acerto MUST NOT contar no orçamento nem gerar linha em `llm_calls`,
mesmo com o orçamento esgotado. Num modelo com cache, o limite do orçamento não barra a chamada, porque o acerto só se
conhece depois; a chamada real que resultar é contada, e o limite do provedor continua valendo. Só respostas com sucesso
são guardadas, e a chave de API MUST NOT aparecer no arquivo.

#### Scenario: Mesma tentativa repetida

- **WHEN** a mesma chamada roda de novo com o mesmo número de tentativa
- **THEN** a resposta vem do cache, sem chamada ao provedor e sem contar no orçamento

#### Scenario: Tentativas independentes

- **WHEN** a mesma chamada roda com outro número de tentativa
- **THEN** o provedor é chamado, e a resposta é independente

#### Scenario: Ferramentas diferentes

- **WHEN** a mesma mensagem é enviada com outra lista de ferramentas
- **THEN** não há acerto

#### Scenario: Produção sem cache

- **WHEN** um modelo é montado sem pedir cache e a mesma mensagem é enviada duas vezes
- **THEN** o provedor é chamado duas vezes

### Requirement: Modelo falso roteirizado

O sistema SHALL oferecer, para os testes, um modelo falso com roteiro: texto, chamadas de ferramenta, saída estruturada,
erro em uma posição do roteiro, travamento e contagem de tokens. Ele MUST passar pelo mesmo orçamento e pelo mesmo
registro dos modelos reais, sem rede, e MUST falhar de forma clara quando o roteiro acabar.

#### Scenario: Chamada de ferramenta roteirizada

- **WHEN** o roteiro traz uma chamada de ferramenta
- **THEN** a resposta do modelo traz essa chamada

#### Scenario: Roteiro acabou

- **WHEN** há mais chamadas do que passos no roteiro
- **THEN** o modelo lança um erro que cita o tamanho do roteiro
