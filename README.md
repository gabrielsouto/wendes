# Wendes 🤖🎙️

**Wendes** é um agente de IA que entra na sua reunião diária (daily) no [Jitsi Meet](https://meet.jit.si), conduz a rodada falando em português, transcreve o que cada pessoa disse, gera a ata automaticamente com um LLM e distribui por e-mail ou webhook.

Toda a IA roda **localmente** (Whisper para transcrição, Piper para voz, LLM via qualquer endpoint OpenAI-compatible) — o único tráfego externo é o próprio áudio da reunião pela infraestrutura do Jitsi.

## O que ele faz

- **Entra sozinho na sala** — funciona com `https://meet.jit.si/<Sala>` (sala pública) e `https://moderated.jitsi.net/<Sala>` (bot como moderador, equipe via guest link).
- **Facilita a daily por voz**: faz a chamada, pergunta a cada pessoa ("O que você fez ontem?", etc. — configurável), respeita timebox, detecta fim de fala por silêncio e faz follow-up de impedimentos via LLM.
- **Comandos via chat**: `/comece`, `/pular`, `/encerrar`, `/permitir <nome>`, `/ajuda` — restritos a admins configuráveis.
- **Modo stealth**: entra mudo, só grava e transcreve; `/fim` no chat encerra e envia a ata.
- **Ata automática**: retranscreve a gravação com um modelo maior após a reunião, resume com o LLM e envia por e-mail (SMTP) e/ou webhook. Reuniões longas são condensadas em blocos antes do resumo final, e respostas que estourem o limite de tokens são continuadas automaticamente.
- **Quem disse o quê**: no modo stealth, os eventos de *dominant speaker* do Jitsi são gravados e casados com a transcrição — a ata atribui as falas às pessoas.
- **Transcrição orientada**: os nomes da equipe e o vocabulário do `config.yaml` viram *hotwords* do Whisper, reduzindo erros com jargões e nomes próprios.
- **Histórico**: cada reunião fica em `data/` com gravação, transcrição (`transcript.txt`/`.json`), falantes (`speakers.json`), log e ata; `wendes resummarize <data>` regera a ata sem reentrar na sala reusando a transcrição salva (`--retranscribe` força nova transcrição; `--no-email` só regera o arquivo).

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
.venv/bin/playwright install chromium

# Baixe uma voz do Piper para models/ (exemplo pt-BR):
mkdir -p models
wget -P models https://huggingface.co/rhasspy/piper-voices/resolve/main/pt/pt_BR/faber/medium/pt_BR-faber-medium.onnx
wget -P models https://huggingface.co/rhasspy/piper-voices/resolve/main/pt/pt_BR/faber/medium/pt_BR-faber-medium.onnx.json
```

## Configuração

```bash
cp config.example.yaml config.yaml   # sala, equipe, perguntas, LLM, e-mail...
cp .env.example .env                 # segredos (token Firebase, senha SMTP)
```

Edite `config.yaml` com a URL da sua sala, os membros da equipe e o endpoint do seu LLM. Todos os campos estão comentados no arquivo.

Para o bot autenticar no Jitsi (obrigatório no `moderated.jitsi.net`, e necessário no `meet.jit.si` se você quiser que ele abra a sala sozinho), gere um refresh token do Firebase uma única vez: veja [docs/OBTER_REFRESH_TOKEN.md](docs/OBTER_REFRESH_TOKEN.md).

## Uso

```bash
scripts/setup_audio.sh        # cria os dispositivos de áudio virtuais (idempotente)
.venv/bin/wendes check        # valida áudio, TTS, LLM e token
scripts/run_wendes.sh join-test   # teste: entra na sala, fala e lista participantes
scripts/run_wendes.sh run         # conduz a daily completa
scripts/run_wendes.sh stealth     # só grava/transcreve; /fim encerra
.venv/bin/wendes resummarize 2026-07-03   # regera a ata de uma data (--retranscribe, --no-email)
```

### Agendamento (systemd)

Para rodar automaticamente nos dias úteis, use as units de usuário em [systemd/](systemd/):

```bash
mkdir -p ~/.config/systemd/user
cp systemd/wendes.{service,timer} ~/.config/systemd/user/
# ajuste WorkingDirectory/ExecStart no .service e o horário no .timer
systemctl --user daemon-reload
systemctl --user enable --now wendes.timer
loginctl enable-linger $USER
```

## Como funciona

O bot abre a sala num Chromium headless (Playwright) sob Xvfb. O áudio flui por dispositivos virtuais do PulseAudio: o TTS (Piper) toca num sink que é remapeado como "microfone" do navegador, e o áudio da sala sai num sink cujo monitor é gravado e transcrito ao vivo pelo faster-whisper. Um LLM (qualquer endpoint OpenAI-compatible) decide follow-ups e escreve a ata; se estiver indisponível, o bot segue com regras fixas e a ata sai com um resumo básico.

```
Piper TTS ──▶ bot_mic_sink ──▶ bot_mic (mic virtual) ──▶ Chromium/Jitsi
Chromium/Jitsi ──▶ bot_out ──▶ gravação + faster-whisper ──▶ LLM ──▶ ata
```

## Limitações conhecidas

- No `meet.jit.si`, o nome da sala é público/adivinhável — para reuniões sensíveis, prefira o `moderated.jitsi.net` (guest link com hash) ou um Jitsi self-hosted (basta trocar `room.url`).
- As frases faladas e os comandos são em pt-BR ([src/wendes/facilitator/prompts_ptbr.py](src/wendes/facilitator/prompts_ptbr.py)) — traduzir para outro idioma é editar um arquivo e trocar a voz do Piper.

## Licença

[MIT](LICENSE)
