# Changelog

Formato: [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/). Versionamento:
[SemVer](https://semver.org/lang/pt-BR/).

## [Unreleased]

## [0.3.0] - 2026-11-24

### Adicionado

- `send_text` aceita uma lista em `texto` (até 10): mensagens picadas para o mesmo chat
  passam por **uma** aprovação só e saem em ordem, com 1 s entre elas. Se uma falhar, as
  seguintes não saem e a resposta diz quantas já foram. `responder_id` vale para a primeira.

### Mudado

- Skill `whatsapp`: a aprovação do cliente é a única etapa. O agente junta as mensagens numa
  chamada e não pede aval no chat antes; antes eram duas aprovações, mais uma por mensagem.
- O campo da aprovação diz "exatamente o que está acima", porque pode cobrir várias mensagens.

## [0.2.0] - 2026-11-23

### Adicionado

- Tools de contato e etiquetas: `save_contact`, `list_labels`, `edit_label` e `label_chat`.
- `UAZAPI_MCP_SEND_DIRS` e `UAZAPI_MCP_SEND_ALLOWED_CHATS` (ver Segurança).
- `tests/test_transcricao.py`: sequência dos três passos, corpo do job, job com erro,
  upload recusado, áudio sem fala, cache e não-cacheamento de erro. Sem rede.
- `tests/test_midia.py`, `tests/test_etiquetas.py` e `tests/test_envio.py`, este com
  aprovação ponta a ponta pelo `mcp.Client` do SDK e a uazapi simulada.

### Mudado

- **Transcrição migrada da ElevenLabs Scribe v2 para a AssemblyAI.** A chave passa a ser
  `ASSEMBLYAI_API_KEY`; `ELEVENLABS_API_KEY` não é mais lida. Modelos em
  `ASSEMBLYAI_SPEECH_MODELS` (lista em ordem de fallback, enviada em `speech_models`;
  vazia = padrão da AssemblyAI) e idioma em `ASSEMBLYAI_LANGUAGE` (default `pt`).
- A AssemblyAI é assíncrona: onde havia um POST agora são três passos — upload do
  arquivo, criação do job e polling com backoff até 3s, teto de 300s por áudio. O
  `upload_url` devolvido pela API é interno e não circula fora do processo.
- `check_config` passa a mostrar `ASSEMBLYAI_API_KEY`, o par modelo/idioma, as pastas
  liberadas para `send_media` e quantos chats dispensam aprovação.
- **Envio exige aprovação do usuário no cliente** (ver Segurança). Bot que enviava sem
  humano passa a receber `NÃO ENVIADO`, a menos que o cliente suporte elicitation ou o
  chat esteja em `UAZAPI_MCP_SEND_ALLOWED_CHATS`.
- POST não é mais repetido automaticamente em falha (antes, até 3 tentativas): envio não é
  idempotente, e a repetição podia duplicar a mensagem.

### Corrigido

- Comentário inline no `.env` (`CHAVE=abc  # nota`) é lido como o python-dotenv lê; antes
  o comentário entrava no valor do token.
- Mídias de mensagens com o mesmo horário não se sobrescrevem mais. Áudios encaminhados
  juntos chegam com o mesmo `messageTimestamp`, e o arquivo era nomeado só por ele: das
  cópias, sobrava uma, e as outras mensagens apontavam para ela. O nome agora leva o id
  da mensagem.
- Download gravado em arquivo temporário e renomeado no fim, para a transcrição nunca
  ler um arquivo pela metade (era a causa de `Too much data for declared Content-Length`).
- O cache de transcrição passa a ser pelo conteúdo do áudio: o mesmo áudio encaminhado
  duas vezes é transcrito uma vez só, inclusive quando as cópias chegam juntas.
- O teto de 300s da transcrição cobre o polling inteiro; uma consulta lenta não estica
  mais o prazo.
- `label_chat` com `definir=" , "` mandava lista vazia e apagava todas as etiquetas do
  chat. Limpar agora exige o `-` explícito.

### Segurança

- **Envio só com aprovação do usuário.** `confirmar=True` era um argumento que o próprio
  modelo preenche, então uma mensagem recebida com prompt injection podia fazer o agente
  enviar sem aval. Agora `send_text` e `send_media` pedem aprovação via MCP elicitation
  (resolver `Resolve`/`Elicit` do SDK, que funciona nas specs antiga e 2026-07-28); cliente
  sem elicitation não envia. `UAZAPI_MCP_SEND_ALLOWED_CHATS` libera destinos de bots autônomos.
- **`send_media` não lê mais qualquer arquivo do disco.** Só de dentro de
  `UAZAPI_MCP_SEND_DIRS` (padrão: a pasta de mídias), com `resolve()` contra `..` e symlinks.
  Antes, a mesma injeção podia enviar `.env`, chaves SSH ou credenciais do usuário.
- Erro da AssemblyAI não leva mais a URL do upload do áudio até o agente.
- `.gitignore` cobre `.env.*` (um por instância), exceto `.env.example`.

## [0.1.0] - 2026-09-16

Primeira versão.

### Adicionado

- 14 tools: leitura (chats, grupos, mensagens, mídias, busca, detalhes, sync), envio
  (texto, mídia, leitura, reação), transcrição avulsa e diagnóstico de configuração.
- Transcrição automática de áudios com ElevenLabs Scribe v2.
- Janelas de tempo em linguagem natural, resolvidas no cliente — a uazapi não filtra por
  data no servidor. Aceita com e sem acento (`mês`, `mes`).
- Envio em duas etapas: prévia com destino resolvido, depois confirmação.
- Multi-instância, com resolução por nome, número ou token.
- Três skills para Claude Code em `skills/`: `whatsapp`, `entender-problema` e
  `resumo-do-dia`.
- Workflow de release: a tag `vX.Y.Z` gera a release no GitHub com wheel e sdist.

### Segurança

- O `.env` do diretório de trabalho não é lido: o cliente MCP inicia o servidor dentro do
  projeto aberto, e um `.env` alheio não pode redirecionar `UAZAPI_SERVER` e o token junto.
- O `fileURL` da uazapi (público, sem autenticação) nunca circula: a mídia vai para o disco
  e só o caminho aparece. O httpx não loga mais as URLs no stderr.

[Unreleased]: https://github.com/pedromuniz-ia/uazapi-mcp/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/pedromuniz-ia/uazapi-mcp/releases/tag/v0.3.0
[0.2.0]: https://github.com/pedromuniz-ia/uazapi-mcp/releases/tag/v0.2.0
[0.1.0]: https://github.com/illumi-ai/uazapi-mcp/releases/tag/v0.1.0
