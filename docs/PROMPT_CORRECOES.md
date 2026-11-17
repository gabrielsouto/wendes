# Prompt — correções de robustez e segurança do Wendes

> Como usar: salve este arquivo como `docs/PROMPT_CORRECOES.md` no repositório `wendes` e peça ao Claude Code: **"leia docs/PROMPT_CORRECOES.md e execute"**. Tudo abaixo da linha é o prompt.

---

Você vai corrigir um conjunto de bugs e lacunas do **Wendes**, um bot em Python que entra em reuniões do Jitsi (Playwright + Chromium sob Xvfb), facilita a daily por voz (Piper TTS), transcreve (faster-whisper) e gera a ata com um LLM local OpenAI-compatible. Antes de começar, leia o `README.md`, o `config.example.yaml` e todo o código em `src/wendes/`, e rode `git log --oneline` para entender as decisões recentes.

## Regras de trabalho

- Crie o branch `fix/robustez` a partir da `main`.
- **Faça um commit por item**, com mensagem em pt-BR no estilo dos commits existentes, e não junte itens no mesmo commit.
- Logs, mensagens de chat e frases faladas continuam em pt-BR.
- Não mude comportamento além do que cada item pede. Se encontrar outro problema, registre-o no resumo final em vez de corrigir.
- **Não reintroduza a atribuição de falas** (dominant speaker casado com a transcrição). Ela foi removida de propósito nos commits `b099533` e `a0f8b1f` por ser pouco confiável. O item 2 apenas alinha a documentação com essa decisão.
- Os testes não podem depender de Jitsi, navegador, PulseAudio, modelos Whisper ou Piper baixados, nem de LLM no ar. Use fakes e `monkeypatch`. Se um import pesado no topo de um módulo atrapalhar os testes, mova-o para dentro da função que o usa.
- Ao fim de cada item, `ruff check src tests` e `pytest` precisam passar.
- Toda opção nova de configuração entra em três lugares: em `config.py` (com default compatível, salvo onde o item diz o contrário), comentada em `config.example.yaml` e no README, quando afetar a instalação ou o uso.

**Ordem de execução:** 0 → 1 → 3 → 4 → 5 → 6 → 7 → 8 → 9 → 2. O README fica por último para já incluir as opções novas.

---

## Item 0 — Infraestrutura de testes e CI

O projeto não tem testes. Monte a base primeiro, para que cada item seguinte já venha com os seus.

- Em `pyproject.toml`:
  - `[project.optional-dependencies] dev = ["pytest>=8", "pytest-asyncio>=0.23", "ruff"]`;
  - `[tool.pytest.ini_options]` com `asyncio_mode = "auto"` e `testpaths = ["tests"]`;
  - `[tool.ruff]` com `target-version = "py311"`, `line-length = 100` e regras `E`, `F`, `W`, `I`. Corrija o que o ruff apontar sem mudar comportamento. Se um aviso só some mudando comportamento, use `# noqa` com justificativa.
- Em `tests/conftest.py`, crie fakes reutilizáveis:
  - `FakeRoom`: guarda as mensagens enviadas (`send_chat`), tem a lista de participantes configurável, implementa `get_display_name`, registra callbacks de chat e de dominant speaker e expõe `emit_chat(pid, text, private=False)` para injetar mensagens.
  - `FakeLlm`: respostas roteirizadas, atraso opcional (com `time.sleep`, para simular chamada bloqueante) e registro dos prompts recebidos.
  - `make_cfg(**overrides)`: monta um `Config` válido sem ler arquivos.
- Escreva testes para as funções puras que já existem:
  - `minutes.summarizer._split_chunks`;
  - `_ask_ata`, cobrindo resposta com `FIM`, resposta sem `FIM` e resposta fora do formato (deve retornar `None`);
  - `facilitator.analysis.analyze_blocker_answer`, cobrindo os marcadores de "sem impedimento", resposta do LLM com sim, com não e com follow-up inválido, e `llm=None` (fallback conservador);
  - `facilitator.stealth.canonical_name`;
  - `speech.stt_batch.normalize_transcript_text` e `stt_hotwords`;
  - `history.store.HistoryStore`: ida e volta de save/load em `tmp_path`, incluindo a migração do schema legado.
- Crie `.github/workflows/ci.yml`: `ubuntu-latest`, Python 3.11 e 3.12, `pip install -e .[dev]` (sem `playwright install` e sem baixar modelos), `ruff check src tests` e `pytest`, com cache do pip.

---

## Item 1 — Fila de áudio sem limite (vazamento de memória no modo stealth)

**Problema.** `audio/capture.py`: `AudioCapture._reader` põe cada pedaço de 0,5 s numa `asyncio.Queue` sem limite (`self.chunks`). Só o `LiveStt` consome essa fila. No modo `stealth` ninguém consome, e a fila cresce cerca de 32 KB/s, o que dá quase 460 MB no limite padrão de 4 h. No modo `run`, a fila também acumula enquanto o bot fala ou espera o LLM (ela é esvaziada no início de cada escuta).

**Correção.**
- Troque `chunks` por um esquema de distribuição para vários consumidores:
  - `subscribe(maxsize: int = 120) -> asyncio.Queue[bytes]` e `unsubscribe(q)`;
  - `_reader` grava no WAV e chama `_dispatch(data)`, que faz `put_nowait` em cada fila inscrita;
  - em `QueueFull`, descarte o pedaço mais antigo (`get_nowait`) e insira o novo, contando os descartes; registre um WARNING com o total no máximo uma vez por minuto.
- Sem inscritos, nada se acumula.
- Mantenha um contador `samples_written` (total de amostras gravadas no WAV), útil para medir tempo pelo relógio do áudio.
- O `Facilitator` (ou o `LiveStt`) se inscreve ao ser criado, e `listen_utterance` passa a ler dessa fila. Mantenha o comportamento de descartar o áudio acumulado enquanto o bot falava, esvaziando a fila no início da escuta.
- O modo `stealth` não se inscreve.

**Testes.**
- `_dispatch` sem inscritos;
- com `maxsize=3` e 5 pedaços, a fila fica com os 3 últimos, em ordem;
- dois inscritos recebem os mesmos pedaços;
- o contador de descartes;
- `samples_written`.

**Aceite manual (vai no resumo para o Gabriel):** rodar o stealth por 10 min e confirmar que a memória do processo Python fica estável (`ps -o rss -p <pid>`).

---

## Item 3 — Ata perdida quando a reunião falha ou o serviço é parado

**Problema.** Em `__main__.py`, se `facilitator.run()` lançar uma exceção (Jitsi caiu, página travou, timeout do Playwright), ela passa pelo `finally` e a transcrição, a ata e a distribuição não rodam. O comentário "mantém referência mesmo se a reunião falhar no meio" não se cumpre. O mesmo vale para `_stealth`. Além disso:
- `systemctl --user stop` manda SIGTERM e o processo morre sem gerar a ata;
- o registro só vai para o SQLite no final, então `resummarize` não consegue recuperar uma reunião interrompida.

**Correção.**
- Extraia o pós-reunião comum para `wendes/pipeline.py`:

  ```python
  def finalize_meeting(cfg, store, run_dir, record, wav_path, *, mode, room_url=None) -> Path
  ```

  A função faz a retranscrição em lote se o WAV tiver mais de 1 s de áudio, `save_transcript`, cria o LLM, chama `generate_ata` ou `generate_stealth_ata` conforme `mode`, `store.save`, `send_ata` e `post_ata`. Uma falha de SMTP ou de webhook vai para o log com `log.exception`, não impede o outro canal e não propaga.
- `MeetingRecord` ganha três campos: `interrupted: bool = False`, `error: str = ""` e `mode: str = ""` (`"run"`, `"stealth"` e, no futuro, `"assist"`). `HistoryStore.load_record` lê os três com `.get`, para que registros antigos continuem abrindo. `cmd_resummarize` escolhe o gerador por `record.mode` e só cai na inferência atual ("sem respostas, então é stealth") quando `mode` estiver vazio.
- Os dois templates da ata mostram no topo, quando a reunião foi interrompida: `⚠ Reunião interrompida: {error}. A ata cobre só o que foi gravado.`
- Em `_run` e `_stealth`:
  - salve um registro provisório no histórico (`store.save(record, "")`) logo depois de `wait_joined`, para que o `resummarize` encontre a reunião mesmo se o processo for morto;
  - envolva a fase da reunião em `try/except Exception`: chame `log.exception`, marque `record.interrupted = True` e `record.error = str(exc)[:200]`;
  - no `finally`, pare a captura e feche o navegador, cada um no seu próprio `try`, tolerando erro;
  - depois, chame **sempre** `finalize_meeting`;
  - no stealth, se a falha acontecer antes de criar o `StealthSession`, a lista de participantes fica vazia, sem `NameError`;
  - `EntryError` continua retornando 1 sem ata, porque não houve reunião.
- **SIGTERM/SIGINT:** instale um handler com `loop.add_signal_handler` que encerra a reunião de forma ordenada. No `run`, ele aciona o mesmo caminho do `/encerrar`; no stealth, chama `session.finished.set()`; antes de entrar na sala, cancela a tarefa principal. Um segundo sinal força a saída. Depois do primeiro sinal, o fluxo segue até `finalize_meeting`.
- **systemd:** em `systemd/wendes@.service`, adicione `TimeoutStopSec=1h` com um comentário (a retranscrição em lote leva minutos). Valide na prática que o Python sobrevive quando o Chromium e o Xvfb morrem junto no stop. Se não sobreviver, ajuste (por exemplo, `KillMode=mixed`) e documente o que fez e por quê.
- **Código de saída:** 0 quando tudo correu bem; 1 quando a fase da reunião falhou, mesmo com a ata gerada, para o systemd registrar a falha.

**Testes.**
- `finalize_meeting` em `tmp_path`, sem WAV e com `llm=None`: a ata sai pelo fallback, é salva e mostra o aviso de interrupção;
- SMTP lançando exceção não impede o webhook (com `monkeypatch` nos dois);
- `load_record` de um registro antigo, sem os campos novos, funciona;
- `resummarize` escolhe o gerador por `mode`.

**Aceite manual:**
- durante um `stealth`, rodar `systemctl --user stop wendes@stealth` gera o `ata.md`;
- durante um `run`, rodar `pkill -f chromium` gera a ata com o aviso de interrupção.

---

## Item 4 — LLM e TTS travando o loop async

**Problema.** Em `facilitator/state_machine.py`, `_person_round` chama `analyze_blocker_answer` de forma síncrona dentro da corrotina, e essa chamada pode levar até `llm.timeout_seconds`. Enquanto isso, o loop de eventos para: `/pular` e `/encerrar` não são processados, e os callbacks do Playwright se acumulam. `Tts.synthesize` também roda o Piper de forma síncrona quando a frase não está em cache.

**Correção.**
- Rode `analyze_blocker_answer` com `asyncio.to_thread`. Faça o mesmo com `self.tts.synthesize` dentro de `say()`.
- Generalize `_listen` num helper `_interruptible(coro)` que disputa qualquer corrotina contra `commands.skip` e `commands.end`, com a mesma semântica de hoje: `end` lança `MeetingEnded`, `skip` retorna `_SKIPPED`. Use-o na escuta e na análise de impedimento. Se a disputa for interrompida, a thread do LLM pode continuar rodando em segundo plano; o resultado dela é ignorado.
- Não migre o `Llm` para `AsyncOpenAI` agora. A mudança deve ser mínima.

**Teste.** Monte um `Facilitator` com:
- `FakeRoom`;
- `FakeLlm` cujo `ask` faz `time.sleep(2)`;
- TTS falso que devolve um caminho qualquer;
- `play_wav` substituído por uma corrotina vazia (`monkeypatch` em `wendes.facilitator.state_machine.play_wav`);
- STT falso que devolve "tenho um impedimento com o deploy".

Acione `commands.end` após 0,1 s. O teste exige que `run()` retorne em menos de 1 s (via `asyncio.wait_for`) e que o registro contenha a pessoa com a resposta parcial.

---

## Item 5 — Qualquer participante consegue virar admin

**Problema.** `ChatCommands._is_admin` (`facilitator/commands.py`) e `StealthSession._is_admin` (`facilitator/stealth.py`) aceitam como admin quem tiver `fuzz.partial_ratio(admin, display) >= 85`. O nome no Jitsi é livre. Numa sala pública do `meet.jit.si`, qualquer pessoa pode entrar como "Fulana da Silva" e mandar `/encerrar`, `/pular`, `/permitir` ou `/fim`. Por causa do `partial_ratio`, até "Fulana Hacker" passa.

**Correção.** Crie `wendes/facilitator/auth.py` com uma classe `AdminAuth`, usada tanto por `ChatCommands` quanto por `StealthSession`, eliminando a lógica duplicada.

- **Casamento por nome** (quando `require_code` está desligado): igualdade exata depois de normalizar (casefold, remoção de acentos com `unicodedata`, espaços colapsados), contra `commands.admins` e os aliases do membro correspondente em `team.members`. Nada de casamento parcial ou fuzzy para permissão. O fuzzy continua apenas para mapear participantes a nomes na ata.
- **Código da sessão:** nova opção `commands.require_code: bool`, com default `true`. É uma mudança intencional de comportamento; registre isso no README.
  - A cada execução, gere um código de 6 dígitos com `secrets`.
  - Entregue-o por três canais: log INFO (aparece no journal), arquivo `run_dir/admin_code.txt` com permissão 0600 e, se o e-mail estiver configurado, e-mail para a nova lista `commands.admin_emails`, enviado logo após entrar na sala.
- **Autenticação:** o admin manda `/auth <código>` **por mensagem privada ao bot** no Jitsi. O id desse participante entra em `granted_ids` para a sessão inteira. A partir daí, os comandos funcionam no chat público normalmente. O id do participante é estável durante a sessão, e o nome deixa de importar.
  - Com `require_code: true`, só o código autentica; o nome é irrelevante.
  - Com `require_code: false`, vale o casamento exato por nome descrito acima.
- **`/auth` no chat público:** o código vazou. Ignore o comando, **gere um código novo** e responda no chat público: "Código exposto no chat público — gerei um novo; use mensagem privada." Entregue o novo código pelos mesmos canais.
- **Limite de tentativas:** depois de 3 códigos errados vindos do mesmo id, ignore as próximas tentativas desse id na sessão e registre um WARNING.
- `/permitir <nome>` continua funcionando, mas só para admin autenticado, como hoje.
- **Ponte de eventos** (`jitsi/room.py`):
  - passe a informar se a mensagem é privada: `botOnChatMessage(id, text, isPrivate)`;
  - os callbacks passam a receber `(pid, text, private)`; atualize `on_chat_message` e todos os chamadores;
  - adicione `JitsiRoom.send_private(pid, text)` usando `APP.conference._room.sendPrivateTextMessage(id, text)`. Confirme na página que esse método existe na versão do lib-jitsi-meet carregada. Se não existir, registre no log e responda no público sem conteúdo sensível.
- `/ajuda` explica o `/auth` em uma linha, sem revelar o código.

**Testes (unitários do `AdminAuth`).**
- Com `require_code: false`, "Fulana Hacker" e "Fulana" não são admin; "fulana da silva" e "Fulána da Silva" são.
- `/auth` com o código certo por mensagem privada concede acesso.
- `/auth` no chat público não concede e troca o código.
- O limite de tentativas funciona.
- `ChatCommands` recusa `/encerrar` de quem não se autenticou.
- `StealthSession` recusa `/fim` de quem não se autenticou.

---

## Item 6 — Chave de API do LLM

**Problema.** `llm/client.py` passa `api_key="none"` fixo. O README diz que funciona com OpenAI ou qualquer endpoint compatível, mas um endpoint que exige chave falha.

**Correção.**
- Adicione `llm_api_key: str = ""` em `Secrets` (variável de ambiente `LLM_API_KEY`) e use `secrets.llm_api_key or "none"`. Atualize o `.env.example`.
- Em `cmd_check`, troque "vLLM ok" e "✘ vLLM" por "LLM ok" e "✘ LLM", mostrando `base_url` e `model`.

**Teste.** Com a variável de ambiente definida, o `Llm` usa a chave (verifique `client.api_key`).

---

## Item 7 — Caminhos e dispositivos fixos no código

**Navegador.** Em `jitsi/browser.py`, `CHROMIUM_PATH = "/usr/bin/chromium-browser"` está fixo, embora o README mande rodar `playwright install chromium`.
- Nova opção `browser.executable_path: str = ""`.
- Vazia: use `/usr/bin/chromium-browser` se existir (comportamento atual, com log INFO); senão, use o Chromium que vem com o Playwright (omita `executable_path`).
- Preenchida: use esse caminho e falhe com mensagem clara se o arquivo não existir.
- `JitsiBrowser` passa a receber o `cfg`.

**STT.** `device="cpu"` está fixo em `LiveStt` e em `transcribe_recording`.
- Nova opção `stt.device: "cpu" | "cuda" | "auto"`, com default `cpu`. Em `auto`, use cuda se `ctranslate2.get_cuda_device_count() > 0`.
- Nova opção `stt.cpu_threads: int = 0`, em que 0 significa todos os núcleos. Hoje o lote usa `os.cpu_count()` e o modo ao vivo usa o default do faster-whisper; unifique.
- No `config.example.yaml`, comente que em cuda o recomendado é `compute_type: float16` (ou `int8_float16`).

**`cmd_check`:** passa a lançar o navegador em headless só para validar o executável, e mostra o dispositivo de STT resolvido.

**Testes.** Resolução do executável (com `monkeypatch` em `Path.exists`) e resolução do dispositivo (com `monkeypatch` na contagem do ctranslate2).

---

## Item 8 — O timebox por pessoa estoura

**Problema.** Em `state_machine.py`, `per_question = max(60, timebox // n)`. Com os defaults (120 s e 3 perguntas), cada pergunta ganha 60 s e a pessoa pode passar de 180 s, sem contar os 20 s extras do aviso e os 45 s do follow-up de impedimento.

**Correção.**
- Crie a função pura `question_budget(timebox_s, n_questions, idx, used_s, min_s) -> float`: o tempo que resta da pessoa dividido pelas perguntas que faltam, com piso em `min_s`.
- Nova opção `meeting.min_seconds_per_question`, com default 30.
- Acumule `used_s` somando os `utt.duration_s`. Os 20 s extras e o follow-up de 45 s continuam existindo, mas entram na conta.
- Documente no `config.example.yaml`.

**Testes.** Casos de `question_budget`: resposta rápida libera mais tempo para as perguntas seguintes, o piso é respeitado e o orçamento esgotado cai no piso.

---

## Item 9 — E-mail da ata em HTML

**Problema.** `distribution/email.py` envia o markdown cru como `text/plain`.

**Correção.**
- Adicione a dependência `markdown` (Python-Markdown, com as extensões `tables` e `sane_lists`).
- Envie `multipart/alternative` com duas partes: o texto em markdown e o HTML, com CSS mínimo inline que funcione no Outlook (fonte, tabelas com borda, espaçamento).
- O assunto inclui o nome da equipe quando estiver configurado: `Ata da Daily — {equipe} — {data}`.

**Teste.** Com `smtplib.SMTP` substituído por um fake que captura a mensagem: há duas partes, e o HTML contém `<h1>` e `<h2>`.

---

## Item 2 — README desalinhado com o código (fazer por último)

**Problema.** O README anuncia o recurso "Quem disse o quê" (dominant speaker casado com a transcrição) e lista um `speakers.json` no histórico. Os dois foram removidos no commit `b099533`.

**Correção.**
- Remova o item e a menção ao `speakers.json`.
- Em "Limitações conhecidas", registre que a ata do modo stealth não atribui falas a pessoas: a diarização foi removida por baixa confiabilidade, e o LLM é instruído a citar nomes apenas quando aparecem no conteúdo da fala.
- Atualize o README com as opções novas dos itens 3, 5, 6, 7 e 8, incluindo o fluxo do `/auth` e o fato de `require_code` vir ligado por padrão.
- Adicione uma seção "Desenvolvimento" com `pip install -e .[dev]`, `pytest` e `ruff check src tests`.

---

## Entrega

Ao terminar, escreva um resumo com:
1. a lista de commits;
2. o que foi testado só em teste unitário e o que precisa de teste manual;
3. qualquer desvio deste prompt e o motivo;
4. problemas encontrados e não corrigidos.

Inclua este roteiro de teste manual para o Gabriel rodar no servidor:

1. `.venv/bin/wendes check`
2. `scripts/run_wendes.sh join-test`:
   - `/auth` por mensagem privada (concede acesso);
   - `/auth` no chat público (o código é trocado);
   - um segundo navegador com o nome de um admin tentando `/encerrar` (deve ser recusado).
3. `stealth` por 10 min observando a memória; depois `/auth` e `/fim`.
4. `systemctl --user stop wendes@stealth` no meio da reunião: a ata deve ser gerada.
5. `run` com `pkill -f chromium` no meio da rodada: a ata deve sair com o aviso de interrupção.
6. `/encerrar` enquanto o LLM analisa um impedimento: a reunião encerra em cerca de 1 s.
