# Wendes 🤖🎙️

**Wendes** é um agente de IA que entra na sua reunião diária (daily) no [Jitsi Meet](https://meet.jit.si), conduz a rodada falando em português, transcreve o que cada pessoa disse, gera a ata automaticamente com um LLM e distribui por e-mail ou webhook.

Toda a IA roda **localmente** (Whisper para transcrição, Piper para voz, LLM via qualquer endpoint OpenAI-compatible) — o único tráfego externo é o próprio áudio da reunião pela infraestrutura do Jitsi.

## O que ele faz

- **Entra sozinho na sala** — funciona com `https://meet.jit.si/<Sala>` (sala pública) e `https://moderated.jitsi.net/<Sala>` (bot como moderador, equipe via guest link).
- **Facilita a daily por voz**: faz a chamada, pergunta a cada pessoa ("O que você fez ontem?", etc. — configurável), respeita timebox, detecta fim de fala por silêncio e faz follow-up de impedimentos via LLM.
- **Comandos via chat**: `/comece`, `/pular`, `/encerrar`, `/permitir <nome>`, `/ajuda` — restritos a admins autenticados com `/auth <código>` por mensagem privada (veja [Comandos e admins](#comandos-e-admins)).
- **Modo stealth**: entra mudo, só grava e transcreve; `/fim` no chat encerra e envia a ata.
- **Ata automática**: retranscreve a gravação com um modelo maior após a reunião, resume com o LLM e envia por e-mail (SMTP, em HTML com o markdown como alternativa) e/ou webhook. Reuniões longas são condensadas em blocos antes do resumo final, e respostas que estourem o limite de tokens são continuadas automaticamente.
- **Ata garantida**: se a reunião cair no meio (Jitsi fora, navegador travado) ou o serviço for parado, a ata é gerada mesmo assim com o que foi gravado, com um aviso de reunião interrompida no topo.
- **Transcrição orientada**: os nomes da equipe e o vocabulário do `config.yaml` viram *hotwords* do Whisper, reduzindo erros com jargões e nomes próprios.
- **Histórico**: cada reunião fica em `data/` com gravação, transcrição (`transcript.txt`/`.json`), log e ata; `wendes resummarize <data>` regera a ata sem reentrar na sala reusando a transcrição salva (`--retranscribe` força nova transcrição; `--no-email` só regera o arquivo). O registro entra no histórico logo que o bot entra na sala, então o `resummarize` encontra até uma reunião cujo processo morreu.

## Requisitos

- Linux com PipeWire/PulseAudio (`pactl`), `xvfb-run` e Python ≥ 3.11
- Um endpoint LLM OpenAI-compatible (vLLM, Ollama, llama.cpp, OpenAI...)
- Uma voz do [Piper](https://github.com/rhasspy/piper/blob/master/VOICES.md) em pt-BR (ou do seu idioma, adaptando as frases)

## Instalação

```bash
git clone https://github.com/gabrielsouto/wendes.git
cd wendes
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/playwright install chromium   # ou aponte browser.executable_path para um Chromium

# Baixe uma voz do Piper para models/ (exemplo pt-BR):
mkdir -p models
wget -P models https://huggingface.co/rhasspy/piper-voices/resolve/main/pt/pt_BR/faber/medium/pt_BR-faber-medium.onnx
wget -P models https://huggingface.co/rhasspy/piper-voices/resolve/main/pt/pt_BR/faber/medium/pt_BR-faber-medium.onnx.json
```

## Configuração

```bash
cp config.example.yaml config.yaml   # sala, equipe, perguntas, LLM, e-mail...
cp .env.example .env                 # segredos (token Firebase, senha SMTP, chave do LLM)
```

Edite `config.yaml` com a URL da sua sala, os membros da equipe e o endpoint do seu LLM. Todos os campos estão comentados no arquivo. Os mais usados:

| Opção | Padrão | O que faz |
|---|---|---|
| `commands.require_code` | `true` | Admin só se autentica com o código da sessão (`/auth` em privado). Veja abaixo. |
| `commands.admin_emails` | `[]` | Quem recebe o código de admin por e-mail (usa o SMTP de `distribution`). |
| `commands.admins` | `[]` | Com `require_code: false`, nomes exatos de admin (ignora acentos e maiúsculas; inclui os aliases do membro). |
| `meeting.timebox_per_person_seconds` | `120` | Tempo total de cada pessoa, repartido entre as perguntas que faltam. O aviso de tempo e o follow-up de impedimento contam no total. |
| `meeting.min_seconds_per_question` | `30` | Piso de escuta por pergunta, mesmo com o tempo da pessoa esgotado. |
| `stt.device` | `cpu` | `cpu`, `cuda` ou `auto` (cuda se houver GPU). Em cuda, use `compute_type: float16` ou `int8_float16`. |
| `stt.cpu_threads` | `0` | Threads do Whisper ao vivo e em lote (`0` = todos os núcleos). |
| `browser.executable_path` | `""` | Chromium a usar. Vazio: `/usr/bin/chromium-browser` se existir, senão o do Playwright. |
| `LLM_API_KEY` (no `.env`) | vazio | Chave do endpoint LLM, para OpenAI e afins. vLLM/Ollama locais dispensam. |

Para o bot autenticar no Jitsi (obrigatório no `moderated.jitsi.net`, e necessário no `meet.jit.si` se você quiser que ele abra a sala sozinho), gere um refresh token do Firebase uma única vez: veja [docs/OBTER_REFRESH_TOKEN.md](docs/OBTER_REFRESH_TOKEN.md).

## Uso

```bash
scripts/setup_audio.sh        # cria os dispositivos de áudio virtuais (idempotente)
.venv/bin/wendes check        # valida áudio, TTS, LLM, navegador, STT e token
scripts/run_wendes.sh join-test   # teste: entra na sala, fala e lista participantes
scripts/run_wendes.sh run         # conduz a daily completa
scripts/run_wendes.sh stealth     # só grava/transcreve; /fim encerra
.venv/bin/wendes resummarize 2026-07-03   # regera a ata de uma data (--retranscribe, --no-email)
```

### Comandos e admins

O nome no Jitsi é livre: qualquer pessoa pode entrar como "Fulana da Silva". Por isso, **por padrão (`commands.require_code: true`) o nome não dá permissão nenhuma** — isto mudou em relação às versões anteriores, que aceitavam nomes parecidos com os de `commands.admins`.

1. A cada execução o bot gera um código de 6 dígitos e o entrega por três canais: no log (`journalctl --user -u wendes@run`), no arquivo `data/<data>/<hora>/admin_code.txt` (permissão 0600) e por e-mail para `commands.admin_emails`, se o SMTP estiver configurado.
2. O admin manda `/auth <código>` **por mensagem privada ao bot** no chat do Jitsi (clique no nome do bot → mensagem privada).
3. A partir daí os comandos dessa pessoa funcionam no chat público até o fim da reunião. A permissão fica presa ao participante, não ao nome.

Se o `/auth` for mandado no chat público, o código é considerado vazado: o bot ignora o comando, gera um código novo, avisa no chat e entrega o novo código pelos mesmos canais. Três códigos errados do mesmo participante bloqueiam novas tentativas dele naquela reunião. `/permitir <nome>` (só para admin autenticado) delega a permissão a outro participante presente.

Para voltar ao modo por nome, use `require_code: false`: vale só o nome exato (sem diferenciar acentos, maiúsculas e espaços extras) de `commands.admins` e os aliases desses membros em `team.members`.

### Em segundo plano / agendamento (systemd)

As units de usuário em [systemd/](systemd/) rodam o bot em segundo plano, sem depender de terminal aberto. A unit é um template: a instância escolhe o modo (`wendes@run` ou `wendes@stealth`).

```bash
mkdir -p ~/.config/systemd/user
cp systemd/wendes@.service systemd/wendes.timer ~/.config/systemd/user/
# ajuste WorkingDirectory/ExecStart no .service, e no .timer o horário e o modo (Unit=)
systemctl --user daemon-reload
loginctl enable-linger $USER   # mantém o serviço vivo sem sessão aberta

systemctl --user start wendes@stealth           # inicia agora, em segundo plano
journalctl --user -u wendes@stealth -f          # acompanha os logs
systemctl --user enable --now wendes.timer      # ou: dispara sozinho nos dias úteis
systemctl --user stop wendes@stealth            # encerra a reunião e gera a ata
```

O `systemctl --user stop` (ou um Ctrl+C no terminal) encerra a reunião como `/encerrar` ou `/fim` e gera a ata antes de sair. O `ExecStop` da unit ([scripts/stop_wendes.sh](scripts/stop_wendes.sh)) manda o SIGTERM só ao processo Python e espera a ata ficar pronta; só depois o systemd derruba o Xvfb e o Chromium. O `TimeoutStopSec=1h` dá tempo para a retranscrição. Um segundo sinal sai na hora, sem ata. O código de saída é 1 quando a reunião caiu no meio, mesmo com a ata gerada, para o systemd registrar a falha.

## Como funciona

O bot abre a sala num Chromium headless (Playwright) sob Xvfb. O áudio flui por dispositivos virtuais do PulseAudio: o TTS (Piper) toca num sink que é remapeado como "microfone" do navegador, e o áudio da sala sai num sink cujo monitor é gravado e transcrito ao vivo pelo faster-whisper. Um LLM (qualquer endpoint OpenAI-compatible) decide follow-ups e escreve a ata; se estiver indisponível, o bot segue com regras fixas e a ata sai com um resumo básico.

```
Piper TTS ──▶ bot_mic_sink ──▶ bot_mic (mic virtual) ──▶ Chromium/Jitsi
Chromium/Jitsi ──▶ bot_out ──▶ gravação + faster-whisper ──▶ LLM ──▶ ata
```

## Limitações conhecidas

- No `meet.jit.si`, o nome da sala é público/adivinhável — para reuniões sensíveis, prefira o `moderated.jitsi.net` (guest link com hash) ou um Jitsi self-hosted (basta trocar `room.url`).
- A ata do modo stealth não atribui falas a pessoas. A diarização (*dominant speaker* do Jitsi casado com a transcrição) foi removida por ser pouco confiável, e o LLM é instruído a citar nomes apenas quando eles aparecem no conteúdo da própria fala.
- Se a versão do Jitsi carregada não tiver `sendPrivateTextMessage`, as respostas ao `/auth` ("autenticado", "código inválido") saem no chat público. Elas nunca contêm o código.
- As frases faladas e os comandos são em pt-BR ([src/wendes/facilitator/prompts_ptbr.py](src/wendes/facilitator/prompts_ptbr.py)) — traduzir para outro idioma é editar um arquivo e trocar a voz do Piper.

## Desenvolvimento

```bash
.venv/bin/pip install -e .[dev]
.venv/bin/pytest
.venv/bin/ruff check src tests
```

Os testes não dependem de Jitsi, navegador, PulseAudio, modelos baixados nem de LLM no ar: usam os fakes de [tests/conftest.py](tests/conftest.py). O CI ([.github/workflows/ci.yml](.github/workflows/ci.yml)) roda o ruff e o pytest em Python 3.11 e 3.12.

## Licença

[MIT](LICENSE)
