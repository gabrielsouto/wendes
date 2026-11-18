# Wendes — Modo Assistente (`wendes assist`)

> Como usar: salve como `docs/SPEC_MODO_ASSISTENTE.md` no repositório e peça ao Claude Code: **"leia docs/SPEC_MODO_ASSISTENTE.md e implemente a Fase N"**, uma fase por vez. Ao fim de cada fase, o Claude Code para e reporta; você valida numa reunião real ou num replay antes de liberar a próxima.
>
> **Pré-requisito:** `docs/PROMPT_CORRECOES.md` já aplicado. Este modo reutiliza:
> - `AudioCapture.subscribe()` e `samples_written`;
> - `AdminAuth` com mensagens privadas;
> - `finalize_meeting` e `MeetingRecord.mode`;
> - o tratamento de SIGTERM;
> - o padrão `to_thread` para chamadas bloqueantes.

---

## 1. Objetivo

Um terceiro modo, além de `run` e `stealth`. O Wendes entra na reunião como **assistente visível** e:
- transcreve continuamente;
- mantém notas vivas;
- responde perguntas feitas no chat ou por voz;
- cria documentos e diagramas durante a conversa;
- acompanha ações e pendências entre reuniões;
- publica tudo numa página local ao vivo.

**Tudo roda na máquina:** Whisper, Piper e o LLM local (OpenAI-compatible) que o projeto já usa. Não depende da daily: serve para qualquer reunião.

Referência: o Claude Meetings da Anthropic (beta, nov/2026). O mapeamento:

| Claude Meetings | Wendes assist (local) |
|---|---|
| Participante visível, com aviso no chat | Já é. Ganha aviso falado e no chat ao entrar (F9) |
| Escreve notas e ações durante a chamada | Notas vivas incrementais (F2) e ações estruturadas (F5) |
| Responde perguntas faladas ou no chat | `/wendes`, `@wendes`, mensagem privada e palavra de ativação por voz (F3) |
| Constrói docs, quadros brancos e slides ao vivo | Artefatos: documento em markdown, diagrama e cronograma em Mermaid, checklist, tabela (F4) |
| Recap a pedido antes do fim | `/resumo` (F6) |
| Página da reunião com notas, transcrição e o que foi criado | Página local ao vivo e exportação estática (F7) |
| Voltar a reuniões passadas e perguntar sobre elas | `wendes ask` e busca no histórico (F3) |
| Auto-join pela agenda | Já coberto pelo timer do systemd; integração com agenda fica fora do escopo |
| Sai da chamada se entra convidado externo | Política para participante desconhecido (F9) |

**Diferencial:** nada sai da máquina além do próprio áudio do Jitsi.

---

## 2. Princípios e restrições

1. **100% local.** Nada de CDN (o JavaScript da página é copiado para dentro do projeto) e nada de API de nuvem.
2. **A atribuição de falas é opcional.** A Fase 0 mede se ela é confiável. Se não for, tudo funciona com o falante "não identificado". Nenhuma funcionalidade pode depender dela.
3. **O LLM local é o gargalo.** O contexto é limitado (`llm.context_tokens`, default 8192) e as requisições são atendidas uma por vez (`llm.max_concurrency`, default 1). Por isso:
   - todo prompt tem orçamento explícito de tokens;
   - toda saída estruturada é JSON validado com pydantic;
   - pedidos humanos têm prioridade sobre o trabalho de fundo.
4. **Nada bloqueante no loop asyncio.** LLM, STT, TTS e consultas pesadas ao SQLite rodam em thread ou executor.
5. **Falhas degradam, não derrubam.**
   - LLM fora do ar: a transcrição e a página seguem, e as perguntas recebem "LLM indisponível no momento".
   - STT atrasado: o trecho ao vivo é descartado e marcado como lacuna. A gravação continua, e a ata final usa a retranscrição em lote.
6. **Transcrição e chat são dados, nunca instruções** (ver F9).
7. **pt-BR** em tudo o que é falado ou escrito para pessoas.
8. **Reutilize** o que já existe: entrada no Jitsi, `JitsiRoom`, `AudioCapture`, `Tts`, `Llm`, `HistoryStore`, peças do `summarizer`, `AdminAuth` e `finalize_meeting`.
9. **Hardware.** Se o LLM rodar na mesma CPU que o Whisper, a transcrição ao vivo vai atrasar. O cenário de referência é o LLM em GPU (vLLM). Documente isso no README.

---

## 3. Arquitetura

```
                          ┌──────────────▶ recording.wav (relógio = amostras gravadas)
Chromium/Jitsi ─áudio─▶ AudioCapture
                          └─subscribe──▶ ContinuousTranscriber ─▶ MeetingState.segments
Eventos da página:                                              │
  audioLevels / dominantSpeaker ─▶ SpeakerTracker ─(atribuição)─┘
  chat público/privado ─────────▶ CommandRouter ◀── VoiceTrigger (palavra de ativação nos segmentos)
  entrada/saída de participantes ─▶ ParticipantPolicy
                                       │
           ┌───────────────┬───────────┼──────────────┬──────────────┐
           ▼               ▼           ▼              ▼              ▼
          Q&A          Artefatos     Resumo         Ações       Controle (/pausar…)
           └───────────────┴─────┬─────┴──────────────┘
                                 ▼
               LlmScheduler (fila com prioridade, 1 por vez) ─▶ LLM local
                                 ▲
                           NotesUpdater (fundo, periódico)

MeetingState ─▶ EventBus ─┬─▶ WebUI (SSE)    ─┬─▶ página ao vivo
                          ├─▶ arquivos JSONL  └─▶ exportação estática no final
                          └─▶ chat / voz (TTS curta)
```

**Módulos novos** em `src/wendes/assistant/`:

| Módulo | Responsabilidade |
|---|---|
| `session.py` | `AssistantSession`: orquestra as tarefas, cuida do ciclo de vida e do encerramento |
| `state.py` | `MeetingState` (fonte única de verdade) e `EventBus` (asyncio, pub/sub) |
| `transcriber.py` | `ContinuousTranscriber`: VAD, segmentação, worker de STT dedicado e controle de atraso |
| `speakers.py` | `SpeakerTracker`: linha do tempo de nível de áudio por participante e atribuição |
| `triggers.py` | `CommandRouter` (chat) e `VoiceTrigger` (palavra de ativação) |
| `llm_queue.py` | `LlmScheduler` e `ask_json` (saída estruturada) |
| `notes.py` | `NotesUpdater`: operações incrementais e compactação |
| `qa.py` | Perguntas: montagem de contexto e recuperação |
| `artifacts.py` | Artefatos com versões |
| `actions.py` | `ActionItem`, persistência, pendências anteriores e webhook |
| `search.py` | Índice FTS5 do histórico |
| `policy.py` | Permissões, participante desconhecido, pausa e retenção |
| `prompts_ptbr.py` | Todos os prompts e frases do modo |
| `web/server.py`, `web/static/` | aiohttp com SSE; página em HTML e JS puro (sem etapa de build) |
| `replay.py` | `FakeRoom` de terminal e fonte de áudio a partir de WAV |

**Arquivos por execução**, em `data/AAAA-MM-DD/HH-MM/`:

| Arquivo | Conteúdo |
|---|---|
| `recording.wav` | Gravação da reunião |
| `live_transcript.jsonl` | Segmentos da transcrição ao vivo |
| `audio_levels.jsonl` | Linha do tempo de níveis de áudio |
| `notes.json` | Notas atuais |
| `notes_ops.jsonl` | Histórico de operações das notas |
| `artifacts/` | Artefatos e suas versões |
| `qa.jsonl` | Perguntas e respostas |
| `llm_calls.jsonl` | Telemetria das chamadas ao LLM |
| `transcript.json` | Transcrição em lote |
| `ata.md` | Ata final |
| `page/index.html` | Página exportada |

O estado da reunião pode ser reconstruído a partir desses arquivos. Se o processo morrer, `wendes resummarize` finaliza a reunião assist (por `record.mode == "assist"`).

---

## 4. Funcionalidades

### F1 — Transcrição contínua com falantes

**Transcrição.**
- O `ContinuousTranscriber` se inscreve no `AudioCapture` e roda o VAD Silero (via `faster_whisper.vad`) sobre os pedaços de 0,5 s.
- Um enunciado fecha com `assistant.stt.silence_s` de silêncio (default 0,8) ou ao atingir `max_utterance_s` (default 20).
- A transcrição usa `stt.live_model` num **único worker dedicado** (`ThreadPoolExecutor(max_workers=1)`), com os hotwords de `stt_hotwords(cfg)`, o nome do bot e os aliases da palavra de ativação.

**Tempo.** Todo timestamp é em segundos desde o início da gravação, **medido pelas amostras gravadas** (`samples_written / 16000`), nunca pelo relógio de parede. O relógio de parede foi uma das causas do desalinhamento da atribuição antiga.

**Segmento.** `{id, start, end, text, speaker, speaker_conf, source}`, com `source` igual a `"live"`, `"batch"` ou `"bot"`. As respostas faladas do bot entram como `source="bot"`, com o nome do bot como falante.

**Atraso.** Se o áudio esperando transcrição passar de `max_lag_s` (default 30):
- descarte os enunciados pendentes mais antigos;
- insira um segmento marcador `[trecho não transcrito ao vivo]`;
- registre um WARNING.

A página mostra o atraso atual.

**SpeakerTracker.**
- Escuta o nível de áudio por participante (`JitsiMeetJS.events.conference.TRACK_AUDIO_LEVEL_CHANGED`, com fallback para `'conference.audioLevelsChanged'`; confirme os nomes na Fase 0) e o dominant speaker.
- Guarda `(t_audio, pid, level)`, em que `t_audio` é a posição atual da captura menos `assistant.speakers.latency_offset_s`, valor calibrado na Fase 0.
- Grava tudo em `audio_levels.jsonl`.

**Atribuição por segmento.**
- Some a energia de cada participante no intervalo `[start, end]`.
- O vencedor precisa ter pelo menos `min_share` (default 0,6) da energia total e amostras suficientes. Caso contrário, `speaker = ""` (não identificado).
- Na transcrição em lote (`word_timestamps=True`), quebre o segmento onde o participante dominante mudar por 1 s ou mais.
- Os nomes passam por `canonical_name`.
- **Não use** a heurística antiga de "dominant speaker no ponto médio do segmento".

**Aceite.**
- O segmento aparece na página até cerca de 5 s depois do fim da fala, no hardware de referência.
- 2 h de sessão sem crescimento de memória.
- A precisão da atribuição está medida e documentada (Fase 0).

### F2 — Notas vivas

O `NotesUpdater` roda a cada `notes.interval_s` (default 120) ou quando acumularem `notes.min_new_chars` (default 1500) de texto novo, o que vier primeiro. Só roda se não houver outro job de notas na fila: um novo gatilho apenas amplia a janela do job pendente.

**Estrutura** (pydantic):

```text
Notes:      topicos[{id, titulo, pontos[{texto, ts}]}], decisoes[{id, texto, ts}],
            acoes[ActionItem], pendencias_anteriores[{id, descricao, status, evidencia_ts}],
            perguntas_abertas[{texto, ts}]
ActionItem: id, descricao, responsavel ("" = não identificado), prazo ("" ou texto),
            ts, status (aberta|concluida|cancelada), origem (reuniao|anterior)
```

**Atualização incremental por operações.** O LLM recebe as notas atuais compactadas (títulos dos tópicos com os últimos pontos, decisões e ações abertas) mais os segmentos novos no formato `[mm:ss] Nome: texto`. Ele devolve **operações**, não o documento inteiro:

```json
{"ops": [
  {"op": "add_point", "topico": "Evento de clientes", "texto": "Local definido: auditório", "ts": 812},
  {"op": "add_decision", "texto": "Data fechada em 12/12", "ts": 845},
  {"op": "add_action", "descricao": "Reservar o auditório", "responsavel": "", "prazo": "sexta", "ts": 860},
  {"op": "update_action", "id": "P3", "status": "concluida", "evidencia_ts": 900},
  {"op": "add_open_question", "texto": "Quem cuida do coffee break?"}
]}
```

O Python aplica as operações de forma determinística:
- um tópico existente é reaproveitado se o título casar com fuzzy ≥ 90;
- operações inválidas são descartadas, com log.

Assim a saída fica curta e o modelo não consegue "perder" notas anteriores.

**Compactação.** A cada `notes.compact_every_s` (default 900), o LLM funde pontos duplicados dentro de cada tópico. O resultado é validado: o número de tópicos não pode cair a ponto de sumir conteúdo e cada ponto antigo precisa estar coberto. Se a validação falhar, as notas antigas são mantidas.

**Responsável.** Só é preenchido quando o segmento tem falante com confiança acima do limiar e o falante se compromete ("eu vou…"), ou quando o nome aparece explicitamente na fala. Caso contrário, fica `""`. É o mesmo espírito do `_NO_ATTRIBUTION` atual.

**Persistência.** `notes.json` é regravado a cada atualização, e `notes_ops.jsonl` guarda todas as operações aplicadas.

**Aceite.**
- No replay de reuniões gravadas, as notas cobrem os assuntos principais.
- Nenhuma operação apaga conteúdo.
- O job de notas nunca atrasa uma pergunta, porque tem prioridade menor.

### F3 — Perguntas (chat, voz e depois da reunião)

**Gatilhos.**
- **Chat:** `/wendes <pergunta>`, `@wendes <pergunta>` ou texto livre em mensagem privada ao bot. Pergunta privada recebe resposta privada.
- **Voz:** um enunciado cujas 1 a 3 primeiras palavras casam com a palavra de ativação (`assistant.wake_word`, default = `room.bot_display_name`, mais os `wake_word_aliases`) com fuzzy ≥ 85, **e** cujo restante tem pelo menos 3 palavras.
  - Se o enunciado for só a palavra de ativação, a pergunta é o próximo enunciado do mesmo falante, ou o próximo enunciado em até 8 s se o falante for desconhecido.
  - Há um intervalo mínimo de 10 s entre ativações.
  - A opção `assistant.voice_trigger` liga e desliga o gatilho por voz.

**Roteamento.** Comandos explícitos não passam pelo roteador. Texto livre passa primeiro por um atalho de palavras-chave:
- "cria/faz/monta um documento/plano" vai para documento;
- "desenha", "diagrama" ou "fluxo" vai para diagrama;
- "resume" ou "resumo" vai para resumo.

Sem casamento, usa um roteador barato no LLM: `{"intencao": "pergunta|documento|diagrama|cronograma|checklist|resumo|acoes|outro"}`.

**Montagem do contexto**, com orçamento derivado de `llm.context_tokens`. Estimativa: tokens = caracteres / 3,5.

| Parte | Orçamento |
|---|---|
| Sistema: papel; responder só com base no contexto; dizer quando não sabe; citar `[mm:ss]`; não inventar autoria; respostas curtas; pt-BR | ≤ 300 tokens |
| Notas atuais compactadas | ≤ 25% |
| Transcrição recente (os últimos minutos que couberem) | ≤ 35% |
| Trechos desta reunião recuperados por busca (útil em reuniões longas) | ≤ 15% |
| Histórico de reuniões anteriores, só com permissão (F9), sempre com a data | ≤ 15% |
| Reserva para a resposta | `max_tokens` = 400 |

Quando falta espaço, corte primeiro a transcrição mais antiga.

**Saída:**

```json
{"resposta_curta": "≤ 2 frases para falar", "resposta": "markdown completo",
 "citacoes": [{"ts": 812}, {"data": "2026-07-03", "ts": 300}]}
```

**Entrega.**
- O chat sempre recebe `resposta`, com o prefixo `↳ {quem perguntou}: {pergunta resumida}`. Acima de cerca de 1500 caracteres, a resposta é cortada com "… (completa na página)".
- Se a pergunta veio por voz e `assistant.voice_answers` está ligado: o bot desmuta, fala a `resposta_curta` com o TTS e muta de novo.
- Se a resposta passar de 2 s, o bot dá um sinal imediato: "🔎 pensando…" no chat, ou "Um instante." por voz.
- Tudo vai para `qa.jsonl` e para a página. A fala do bot entra na transcrição com `source="bot"`.
- **Quem perguntou:** no chat, o participante; por voz, o falante atribuído ou "alguém na sala (não identificado)".

**Depois da reunião:** `wendes ask "pergunta" [--date AAAA-MM-DD [--hora HH:MM]]`.
- Sem data, busca em todo o histórico.
- Com data, usa as notas e a transcrição daquela reunião.
- É o mesmo motor de perguntas, sem Jitsi.

**Índice do histórico.**
- Tabelas FTS5 `seg_fts(meeting_key, ts, speaker, text)` e `ata_fts(meeting_key, text)`, com tokenizer `unicode61 remove_diacritics 2`.
- São populadas no `finalize_meeting`. O comando `wendes reindex` indexa as reuniões antigas a partir de `transcript.json` e `ata.md`.
- Consulta: palavras-chave sem stopwords em pt, `MATCH` com OR, ordenação por bm25, e os top-k trechos com 2 segmentos vizinhos de cada lado.

**Aceite.**
- No replay, perguntas sobre fatos da transcrição recebem a resposta correta, com citação.
- Uma pergunta sem resposta no contexto recebe "não sei", e não uma invenção.
- Opcional: um conjunto `evals/qa.yaml` (pergunta e fatos esperados) rodado contra um replay, para comparar prompts e modelos.

### F4 — Artefatos ao vivo (os "quadros brancos" locais)

**Tipos:**
- `doc`: markdown;
- `diagrama`: Mermaid (flowchart, sequência, mindmap, estados);
- `cronograma`: Mermaid gantt;
- `checklist`: lista de tarefas em markdown;
- `tabela`: tabela em markdown.

**Criação e atualização.**
- Criar: `/doc`, `/diagrama`, `/cronograma` ou `/checklist`, seguidos do pedido; ou por voz e texto livre, via roteador.
- Atualizar: `/doc #2 <alteração>`, ou pelo título com fuzzy ("atualiza o plano do evento com…").
- Na atualização, o LLM recebe a versão mais recente, o pedido e o contexto relevante, e devolve o conteúdo inteiro novo. Artefatos são pequenos: limite de cerca de 1500 tokens.

**Saída do LLM:** `{"tipo", "titulo", "conteudo"}`.

**Validação.**
- Markdown não pode vir vazio.
- No Mermaid, remova as cercas de código; a primeira linha não vazia precisa começar com `flowchart`, `graph`, `sequenceDiagram`, `gantt`, `mindmap`, `timeline` ou `stateDiagram-v2`.
- Se o Mermaid falhar ao renderizar no navegador, a página envia `POST /api/artifacts/{id}/render-error` com o erro. O servidor pede uma correção ao LLM uma única vez, com prioridade baixa, gerando uma nova versão. Se falhar de novo, a página mostra o código-fonte.

**Versões.**
- Ficam em `artifacts/{id}-{slug}/v{n}.md` (ou `.mmd`), com um índice em `artifacts.json`.
- Cada versão registra o autor (Wendes, ou quem editou na página) e o pedido que a gerou.

**Aviso no chat:** "📝 Criei «{titulo}» (#{id}) — está na página." O link só vai para quem pode recebê-lo (F9).

**Edição na página** (opcional, `assistant.web.allow_edit`): uma área de texto que salva uma nova versão com o autor informado na página. O nome do autor fica no `localStorage`, sempre dentro de `try/catch`. Revisões do LLM sempre partem da versão mais recente, inclusive edições humanas.

**No final:** os artefatos são listados na ata e anexados ao e-mail como `.md` (`distribution.attach_artifacts`).

**Aceite.**
- "Wendes, desenha o fluxo de aprovação que a gente acabou de discutir" gera um diagrama renderizado na página em até 30 s.
- Uma edição feita na página é preservada pela atualização seguinte do LLM.

### F5 — Ações estruturadas e pendências da reunião anterior

**Persistência.** Tabela `action_items`:

```text
id, meeting_date, meeting_started_at, room_url, descricao, responsavel, prazo, status,
ts, resolvido_em_date, resolvido_em_started_at, evidencia
```

Inclui a migração do schema. As ações das notas são gravadas no `finalize_meeting`.

**Ao entrar.**
- Carregue as ações abertas de reuniões anteriores da mesma sala, dentro de `actions.lookback_days` (default 14).
- Poste no chat: "Pendências abertas: …", com no máximo 10 itens; o restante fica na página.
- `/pendencias` reposta a lista.

**Durante.**
- O `NotesUpdater` recebe as pendências abertas, com ids `P1…Pn`, e pode emitir `update_action` para elas, citando o trecho que serve de evidência.
- A página mostra o status de cada pendência anterior: resolvida nesta reunião, mencionada ou sem menção.

**`/cobrar`** (opcional): o bot lê por voz as pendências com responsável ("Fulana, a pendência X da reunião de terça foi resolvida?"), escuta por 20 s e classifica a resposta com o LLM.

**Webhook de ações:** `distribution.actions_webhook_url` recebe um POST no final.

```json
{"reuniao": {"data": "2026-11-18", "inicio": "09:30", "sala": "…", "equipe": "…"},
 "acoes": [{"id": 12, "descricao": "…", "responsavel": "…", "prazo": "…", "status": "aberta", "ts": 860}],
 "pendencias_resolvidas": [{"id": 7, "descricao": "…", "evidencia": "…"}]}
```

- São 3 tentativas com backoff. O payload fica salvo em `actions_payload.json`, e `wendes send-actions <data> [--hora]` reenvia.
- Adaptadores específicos (GLPI, Jira) ficam fora do escopo. O payload documentado basta para um n8n ou um script consumir.

**Aceite.**
- A reunião 2 mostra as pendências da reunião 1.
- A resolução por fala funciona no replay.
- O payload é validado por pydantic nos testes.

### F6 — Resumo sob demanda

- `/resumo`, ou "Wendes, faz um resumo" por voz: gera, **a partir das notas** (barato, sem reler a transcrição), um recap de até 12 linhas com tópicos, decisões e ações com responsável. Vai para o chat; se o pedido foi por voz, o bot fala 2 ou 3 frases.
- `/acoes`: posta a lista atual de ações.

**Aceite:** menos de 10 s com as notas já atualizadas.

### F7 — Página da reunião e modo replay

**Servidor.** aiohttp (dependência nova) no mesmo processo e loop.

| Rota | Função |
|---|---|
| `GET /m/{run_id}` | Página |
| `GET /m/{run_id}/events` | Stream SSE |
| `GET /m/{run_id}/state` | Estado completo em JSON, para reconexão |
| `POST /m/{run_id}/artifacts/{id}` | Edição de artefato, se permitida |
| `POST /m/{run_id}/artifacts/{id}/render-error` | Erro de renderização do Mermaid |

**Token.**
- Gerado com `secrets.token_urlsafe(32)` a cada execução.
- Exigido em toda requisição: na URL no primeiro acesso, depois num cookie `HttpOnly`. Sem token, a resposta é 403.
- Configuração: `assistant.web.host` (default `127.0.0.1`), `port` (default 8765) e `public_base_url` para montar os links (por exemplo, `http://servidor.lan:8765`).

**JavaScript copiado para o projeto.**
- Bibliotecas: marked, DOMPurify e mermaid, em versões fixas.
- O script `scripts/vendor_js.sh` baixa uma vez para `web/static/vendor/`, junto com as licenças, e os arquivos entram no package data.
- Todo markdown passa pelo DOMPurify. O Mermaid roda com `securityLevel: 'strict'`.

**Layout da página.**
- **Cabeçalho:** sala, início, duração, status (● gravando ou ⏸ pausado), atraso do STT, fila do LLM e participantes.
- **Transcrição:** rolagem automática com botão para pausar, busca, falante colorido e timestamps clicáveis.
- **Notas:** tópicos e decisões.
- **Ações:** tabela de ações e pendências anteriores.
- **Artefatos:** abas, versões, baixar `.md`/`.mmd` e copiar.
- **Perguntas:** respostas com citações clicáveis que pulam para o segmento.

A página é responsiva, tem tema claro e escuro via `prefers-color-scheme` e usa JS puro com ES modules, sem etapa de build.

**Exportação.** No final, `page/index.html` é gerado autocontido, com o estado em JSON e o JavaScript embutidos, para abrir offline. O caminho aparece na ata.

**`wendes serve`.** Servidor persistente do histórico (Fase 6): lista as reuniões, abre as páginas exportadas, busca no FTS e responde perguntas com o motor da F3. O token vem de `WENDES_WEB_TOKEN` no `.env`.

**Replay.** `wendes assist --replay <data> [--hora HH:MM] [--speed 4]` roda o pipeline inteiro (transcrição, falantes a partir de `audio_levels.jsonl` quando existir, notas, perguntas, artefatos e página) a partir do `recording.wav`.
- Usa uma `FakeRoom` de terminal: o "chat" é impresso na tela e os comandos são lidos do stdin (por exemplo, `/resumo` ou `/wendes quem ficou de revisar o PR?`).
- `--speed` controla o ritmo de entrega do áudio; 0 significa o mais rápido possível.
- **É a ferramenta principal para ajustar os prompts sem precisar de reunião.**

**Aceite.**
- A página atualiza ao vivo em outra máquina da rede via `public_base_url`.
- Recarregar a página retoma o estado.
- A exportação abre offline no Firefox e no Chrome.

### F8 — Ata final

No `finalize_meeting`, com `mode="assist"`:

1. Retranscrição em lote (modelo grande, `word_timestamps=True`) e atribuição com a mesma linha do tempo da F1. Resultado em `transcript.json`, com falante e confiança.
2. Notas finais: rode o `NotesUpdater` offline sobre a transcrição em lote, em blocos, reaproveitando o mesmo código de operações. As notas ao vivo ficam em `notes_live.json`. Para manter válidas as referências feitas no chat, as ações finais preservam os ids das ações ao vivo quando a descrição casar com fuzzy ≥ 85. **Se isso ficar complexo demais, use as notas ao vivo como base e a transcrição em lote só para busca. Decida e justifique no relatório da fase.**
3. Template `ata_assist.md.j2`:
   - cabeçalho com data, início, duração, sala e participantes;
   - resumo de até 2 parágrafos, gerado pelo LLM a partir das notas finais;
   - tópicos;
   - decisões;
   - ações em tabela (#, descrição, responsável, prazo);
   - pendências anteriores (resolvidas e ainda abertas);
   - artefatos criados;
   - perguntas respondidas;
   - aviso de interrupção, se houver;
   - rodapé com o caminho da página.
4. Indexação no FTS, gravação das `action_items`, e-mail (HTML, com os artefatos anexados), webhook da ata (já existente) e webhook de ações (F5).

### F9 — Privacidade, permissões e segurança

**Aviso ao entrar, falado e no chat** (texto configurável): "Olá, sou o Wendes, assistente desta reunião. Estou transcrevendo para gerar notas e a ata. Qualquer pessoa pode pausar com /pausar no chat."

**Pausa.**
- `/pausar` vale para qualquer participante: a privacidade vence.
- `/retomar` só para admin autenticado (`AdminAuth`).
- Durante a pausa, a captura grava **silêncio digital** no WAV, para não desalinhar a linha do tempo, e não alimenta a transcrição nem o `SpeakerTracker`.
- Entra um segmento marcador `[gravação pausada por {nome} — mm:ss a mm:ss]`, e a página mostra o status.

**Permissões** (`assistant.permissions`):

| Opção | Valores | Default | Observação |
|---|---|---|---|
| `ask` | `all`, `members`, `admins` | `members` | `members` = roster por nome normalizado, ou admin |
| `history` | `admins`, `members`, `none` | `admins` | Perguntas sobre reuniões anteriores só pelo chat, de quem tem permissão; **nunca por voz**, porque a identidade pela voz não é confiável |
| `artifacts` | `all`, `members`, `admins` | `members` | |
| link da página | — | — | Enviado só por mensagem privada a admins autenticados (`/link`), porque a página contém a transcrição inteira |

**Participante desconhecido** (nome fora do roster, excluindo o bot): `assistant.unknown_participant`.

| Valor | Comportamento |
|---|---|
| `allow` | Nada muda |
| `warn` (default) | Aviso no chat; o acesso ao histórico fica desligado enquanto a pessoa estiver na sala |
| `pause` | Pausa automática até a pessoa sair ou um admin mandar `/retomar` |
| `leave` | Finaliza e sai, como o Claude Meetings faz com convidados externos |

**Injeção de prompt.**
- Transcrição e chat entram nos prompts dentro de delimitadores (`<transcricao>…</transcricao>`), com a instrução de que são dados.
- O LLM não tem ferramentas: a saída dele só vai para o chat, a página e os arquivos.
- O payload do webhook é montado apenas a partir de campos validados do `ActionItem`.
- Markdown sempre sanitizado e Mermaid em modo estrito.

**Retenção** (vale para todos os modos):
- `data_retention.recording_days` (default 30; 0 = manter);
- `data_retention.transcript_days` (default 0 = manter);
- `wendes purge [--dry-run]`, com execução automática no início de cada execução;
- `ata.md` e `notes.json` nunca são apagados.

**Logs:** no modo assist, o nível INFO registra só tamanhos e ids; o texto da transcrição fica em DEBUG.

### F10 — (opcional) A página na tela da reunião

Compartilhar a página ao vivo como tela do bot no Jitsi, para todos verem notas e artefatos sem abrir link. É o mais perto do tile do Claude mostrando o trabalho.

**Caminho a validar:**
- uma segunda página no mesmo Chromium, com título fixo;
- flags `--auto-select-desktop-capture-source="<título>"` e `--enable-usermedia-screen-capturing`;
- chamada a `APP.conference.toggleScreenSharing()`;
- Xvfb com `-s "-screen 0 1920x1080x24"`;
- comandos `/tela` e `/tela off`.

Se não funcionar de forma confiável no meet.jit.si, documente e abandone: o recurso é opcional.

---

## 5. Comandos

| Comando | Quem pode | Efeito |
|---|---|---|
| `/auth <código>` (privado) | qualquer um | Autentica como admin (do PROMPT_CORRECOES) |
| `/wendes <pergunta>`, `@wendes …`, texto em mensagem privada | `permissions.ask` | Pergunta (F3) |
| "Wendes, …" (voz) | `permissions.ask` | Pergunta ou pedido por voz, só sobre a reunião atual |
| `/doc`, `/diagrama`, `/cronograma`, `/checklist` `<pedido>` | `permissions.artifacts` | Cria ou atualiza artefato (`#id` para atualizar) |
| `/resumo` | `permissions.ask` | Recap (F6) |
| `/acoes` | `permissions.ask` | Lista as ações atuais |
| `/pendencias` | `permissions.ask` | Pendências anteriores |
| `/cobrar` | admin | Cobra as pendências por voz (opcional) |
| `/pausar` | qualquer um | Pausa a gravação e a transcrição |
| `/retomar` | admin | Retoma |
| `/link` | admin | Link da página por mensagem privada |
| `/tela`, `/tela off` | admin | Compartilha a página (F10, opcional) |
| `/fim` | admin | Encerra e gera a ata |
| `/ajuda` | qualquer um | Lista os comandos visíveis para quem pediu |

---

## 6. Configuração (acrescentar ao `config.example.yaml`)

```yaml
assistant:
  greeting: "Olá, sou o Wendes, assistente desta reunião. Estou transcrevendo para gerar notas e a ata. Qualquer pessoa pode pausar com /pausar no chat."
  wake_word: ""                  # vazio = room.bot_display_name
  wake_word_aliases: ["vendes", "wendis", "uendes"]
  voice_trigger: true
  voice_answers: true
  stt:
    silence_s: 0.8
    max_utterance_s: 20
    max_lag_s: 30
  speakers:
    enabled: false               # a Fase 0 decide o default
    latency_offset_s: 0.0        # calibrado na Fase 0
    min_share: 0.6
  notes:
    interval_s: 120
    min_new_chars: 1500
    compact_every_s: 900
  permissions:
    ask: members
    history: admins
    artifacts: members
  unknown_participant: warn      # allow | warn | pause | leave
  web:
    host: 127.0.0.1
    port: 8765
    public_base_url: ""
    allow_edit: false

actions:
  lookback_days: 14

llm:
  context_tokens: 8192
  max_concurrency: 1
  structured_output: json_object # none | json_object | vllm_guided | ollama_format

distribution:
  actions_webhook_url: ""
  attach_artifacts: true

data_retention:
  recording_days: 30
  transcript_days: 0
```

---

## 7. Regras práticas para o LLM local

**`LlmScheduler`.**
- Fila `asyncio.PriorityQueue` com três prioridades:
  - 0: pedido humano (pergunta, artefato, resumo);
  - 1: notas incrementais;
  - 2: reparo de Mermaid, compactação, classificação de pendências.
- No máximo `max_concurrency` requisições simultâneas, com timeout por job.
- Jobs de notas se fundem: um novo gatilho amplia o job pendente em vez de criar outro.
- Tudo é cancelado no encerramento. O tamanho da fila aparece na página.

**`ask_json(prompt, schema: type[BaseModel])`**, conforme `llm.structured_output`:

| Valor | Implementação |
|---|---|
| `json_object` | `response_format={"type": "json_object"}` |
| `vllm_guided` | `extra_body` com o JSON schema do pydantic. Verifique a chave aceita pela versão do vLLM instalada (`guided_json` nas versões mais antigas, `structured_outputs` nas mais novas) |
| `ollama_format` | `extra_body={"format": schema}` |
| `none` | Só pelo prompt |

Em todos os casos:
- extraia o primeiro objeto JSON da resposta, tolerando cercas ` ```json `;
- valide com pydantic;
- se falhar, faça 1 nova tentativa incluindo o erro de validação;
- se falhar de novo, use o fallback da funcionalidade (notas: pula a rodada; pergunta: "não consegui formular a resposta agora").

**Prompts.**
- Mantenha a regra do `llm/client.py`: system prompt sempre presente, pt-BR, sem raciocínio exposto (o gemma vaza raciocínio em inglês sem isso).
- Todos os prompts ficam em `assistant/prompts_ptbr.py`, cada um com um exemplo curto do JSON esperado.

**Orçamento:** `estimate_tokens(text) = ceil(len(text) / 3.5)` e `fit(partes, orçamento)`, que corta primeiro a transcrição mais antiga.

**Telemetria:** cada chamada vai para `llm_calls.jsonl` com tipo, tokens estimados, latência e ok/falha, sem o conteúdo. Serve para calibrar intervalos e orçamentos.

---

## 8. Fases

Cada fase termina com `ruff` e `pytest` passando, um commit por bloco lógico (mensagens em pt-BR) e um relatório curto: o que foi feito, como testar manualmente e as decisões tomadas. **Pare ao fim de cada fase.**

### Fase 0 — Spike de falantes (sem código de produção)

**Objetivo:** decidir com números se a atribuição de falas entra.

- `scripts/spike_speakers.py`: entra na sala como o stealth e, por 10 min, grava o WAV e registra todo evento de nível de áudio e de dominant speaker com `t_audio`. No terminal, mostra eventos por segundo de cada participante.
- O Gabriel faz uma reunião de teste com 3 ou mais pessoas e depois rotula quem falou quando no Audacity (faixa de rótulos exportada como `labels.txt`: início, fim e nome, separados por tab).
- `scripts/eval_speakers.py` mede a precisão, por tempo de fala e por segmento, de três abordagens:
  - (a) a heurística antiga (dominant speaker no ponto médio);
  - (b) integração dos níveis de áudio, testando `latency_offset_s` de −1,0 a +1,0 s em passos de 0,1;
  - (c) a abordagem (b) com quebra por palavra.
- Verifique também:
  - os nomes dos eventos disponíveis (gravar um dump de `JitsiMeetJS.events.conference`);
  - se os níveis chegam para todos os participantes no meet.jit.si, já que reescrita de SSRC e roteamento só dos mais altos podem interferir;
  - se `sendPrivateTextMessage` existe.
- **Entrega:** `docs/SPIKE_FALANTES.md` com os números, o offset escolhido e a recomendação.
- **Regra de decisão:** se (b) ou (c) atingir pelo menos 85% de precisão por tempo de fala, `speakers.enabled: true` vira o default. Senão, fica `false`, e o resto do modo não muda.

### Fase 1 — Esqueleto do `wendes assist`

**Entregáveis.**
- CLI `assist`, com `--url`, `--max-hours`, `--replay` e `--speed`.
- `AssistantSession`, `MeetingState`, `EventBus`, `ContinuousTranscriber` e `SpeakerTracker` (se habilitado).
- Servidor web com a página mostrando por enquanto só a transcrição e o status.
- Aviso de entrada; comandos `/pausar`, `/retomar`, `/link`, `/fim` e `/ajuda`.
- Política de participante desconhecido, retenção, `finalize_meeting` com uma ata provisória no estilo do stealth e replay com `FakeRoom`.

**Testes.**
- Segmentação com áudio sintético (rajadas senoidais com numpy, intercaladas com silêncio) e uma função de transcrição injetada no lugar do Whisper.
- Descarte por atraso.
- A pausa grava silêncio e mantém a linha do tempo.
- A matemática da atribuição.
- O token das rotas web (cliente de teste do aiohttp).
- O SSE emite os eventos.
- O replay de ponta a ponta com transcrição falsa.

**Manual:** reunião real de 30 min, com a página aberta em outra máquina.

### Fase 2 — Notas vivas e resumo

**Entregáveis.**
- `LlmScheduler`, `ask_json` e `NotesUpdater` (operações e compactação).
- Colunas de notas e ações na página.
- `/resumo`, `/acoes` e `ata_assist.md.j2` baseada nas notas.

**Testes.**
- Aplicação das operações (válidas, inválidas e tópico reaproveitado por fuzzy).
- Fusão de jobs de notas e ordem de prioridade do scheduler.
- `ask_json` com lixo seguido de JSON válido (nova tentativa) e lixo duas vezes (fallback).
- `fit` respeitando o orçamento.

**Manual:** replay de 2 ou 3 dailies gravadas; o Gabriel avalia a qualidade das notas e os prompts são ajustados.

### Fase 3 — Perguntas

**Entregáveis.**
- Roteador e motor de perguntas com montagem de contexto.
- FTS5 e `wendes reindex`.
- `/wendes`, `@wendes` e mensagem privada.
- Palavra de ativação por voz com resposta pelo TTS.
- Permissões, `wendes ask` e coluna de perguntas na página.

**Testes.**
- Palavra de ativação, com casos positivos e negativos (por exemplo, "você vende isso?" não pode ativar).
- Pergunta que chega depois da palavra de ativação isolada.
- Matriz de permissões; histórico bloqueado por voz e quando há desconhecido na sala.
- Ordem e orçamento do contexto montado.
- Busca FTS com acentos.

### Fase 4 — Ações e pendências

**Entregáveis.**
- Tabela `action_items` com migração.
- Pendências anteriores carregadas na entrada; `/pendencias`; operações `P*`.
- Webhook de ações, `send-actions` e seção da ata.

**Testes.**
- Consulta das pendências por janela de dias e por sala.
- Atualização de status pelas operações.
- Schema do payload.
- Novas tentativas do webhook com transporte falso do httpx.

### Fase 5 — Artefatos

**Entregáveis.**
- Tipos, comandos, intenções do roteador e versões.
- Aba de artefatos na página com marked, mermaid e DOMPurify.
- Reparo do Mermaid, edição opcional, anexos no e-mail e lista na ata.

**Testes.**
- Validação do Mermaid e remoção das cercas.
- Arquivos de versão.
- Atualização por `#id` e por título.
- Autenticação da API de edição.
- Fluxo de reparo com `FakeLlm`.

### Fase 6 — Pós-reunião e acabamento

**Entregáveis.**
- Pipeline final da ata (lote, notas offline e decisão documentada).
- Exportação estática da página e `wendes serve`.
- `wendes check` validando o modo assist: aiohttp instalado, arquivos JS presentes e porta livre.
- Seção "Modo assistente" no README e `config.example.yaml` atualizado.

**Testes.**
- A página exportada contém o estado e não referencia nenhum `<script src="http…">`.
- Autenticação do `serve`.

### Fase 7 — (opcional) Tela compartilhada (F10)

---

## 9. Infraestrutura de testes

- Estender a `FakeRoom`: injetar níveis de áudio, mensagens privadas, entrada e saída de participantes.
- `FakeLlm` com respostas JSON roteirizadas por tipo de chamada.
- `FakeTranscribe`: mapeia o índice do enunciado para um texto.
- Gerador de WAV sintético.
- **Nenhum teste usa rede, modelos ou navegador.** O CI continua verde.

---

## 10. Fora do escopo

- Vídeo ou análise visual.
- Várias salas ao mesmo tempo.
- Outras plataformas além do Jitsi.
- Integração com agenda.
- Adaptadores específicos de GLPI ou Jira (só o webhook genérico).
- Tradução.
- Diarização por embeddings de voz (pyannote ou similar): evolução possível se a Fase 0 reprovar a atribuição por nível de áudio.

---

## 11. Definição de pronto do modo

- `ruff` e `pytest` verdes.
- README e `config.example.yaml` atualizados.
- `wendes check` cobre o modo assist.
- Uma reunião real de 1 h roda sem erros, com memória estável, ata gerada, página exportada abrindo offline e pelo menos uma pergunta, um artefato e um `/resumo` funcionando ao vivo.
