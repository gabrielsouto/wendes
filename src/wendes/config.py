"""Carrega config.yaml + .env em um objeto tipado."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class RoomConfig(BaseModel):
    url: str
    bot_display_name: str = "Wendes"
    # Quanto esperar a conferência abrir. No meet.jit.si sem token o bot
    # aguarda um humano autenticado abrir a sala — use um valor maior.
    join_timeout_seconds: int = 90


class Member(BaseModel):
    name: str
    aliases: list[str] = []


class TeamConfig(BaseModel):
    name: str = ""  # nome da equipe, usado no prompt do LLM e na ata
    members: list[Member] = []


class MeetingConfig(BaseModel):
    timebox_per_person_seconds: int = 120
    # Piso de escuta por pergunta, mesmo com o timebox da pessoa esgotado
    min_seconds_per_question: int = 30
    end_of_turn_silence_seconds: float = 2.0
    quorum: int = 2
    wait_quorum_minutes: int = 10
    questions: list[str] = [
        "O que você fez ontem?",
        "O que vai fazer hoje?",
        "Tem algum impedimento?",
    ]


class SttConfig(BaseModel):
    live_model: str = "small"
    batch_model: str = "medium"
    compute_type: str = "int8"
    # "cpu", "cuda" ou "auto" (cuda se o ctranslate2 enxergar alguma GPU)
    device: Literal["cpu", "cuda", "auto"] = "cpu"
    # Threads de CPU do Whisper (ao vivo e em lote); 0 = todos os núcleos
    cpu_threads: int = 0
    download_root: str = "models/whisper"
    vocabulary: str = ""
    # Termos que o modelo realmente erra, injetados em cada janela de áudio.
    # Lista curta (~10-15) — longa demais dilui o efeito e induz falsos positivos.
    hotwords: list[str] = []
    # Correções pós-STT: {"grafia errada recorrente": "forma correta"}.
    # Casadas como palavra inteira, sem diferenciar maiúsculas.
    corrections: dict[str, str] = {}


class TtsConfig(BaseModel):
    voice_path: str = "models/pt_BR-faber-medium.onnx"
    # Correções de pronúncia adicionais: {"termo escrito": "como falar"}
    phonetic: dict[str, str] = {}


class LlmConfig(BaseModel):
    base_url: str
    model: str
    timeout_seconds: int = 30
    # Janela de contexto do modelo local: os prompts do modo assist têm
    # orçamento derivado daqui (tokens ≈ caracteres / 3,5).
    context_tokens: int = 8192
    # Requisições simultâneas ao LLM no modo assist (vLLM/Ollama locais: 1).
    max_concurrency: int = 1
    # Como pedir JSON ao servidor: none | json_object | vllm_guided | ollama_format
    structured_output: Literal["none", "json_object", "vllm_guided", "ollama_format"] = (
        "json_object"
    )
    # Chave do extra_body no vllm_guided: "guided_json" (vLLM antigo) ou
    # "structured_outputs" (vLLM novo, vira {"json": schema}).
    vllm_guided_key: Literal["guided_json", "structured_outputs"] = "guided_json"


class BrowserConfig(BaseModel):
    # Vazio: /usr/bin/chromium-browser se existir, senão o Chromium do Playwright
    executable_path: str = ""


class CommandsConfig(BaseModel):
    admins: list[str] = []
    start_on_command: bool = True  # espera /comece antes de iniciar a rodada
    # Admin só se autentica com o código da sessão (/auth em privado). Com
    # false, vale o nome exato (sem acentos/maiúsculas) de admins e aliases.
    require_code: bool = True
    # Quem recebe por e-mail o código de admin de cada execução (usa o SMTP
    # de distribution.smtp_host; independe de email_enabled).
    admin_emails: list[str] = []


class DistributionConfig(BaseModel):
    email_enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 25
    email_from: str = ""
    email_to: list[str] = []
    webhook_url: str = ""
    # Modo assist: POST com as ações estruturadas no fim da reunião (F5)
    actions_webhook_url: str = ""
    # Modo assist: anexa os artefatos (.md/.mmd) ao e-mail da ata
    attach_artifacts: bool = True


class AssistSttConfig(BaseModel):
    silence_s: float = 0.8  # silêncio que fecha um enunciado
    max_utterance_s: float = 20.0
    max_lag_s: float = 30.0  # acima disso, descarta o mais antigo e marca lacuna


class SpeakersConfig(BaseModel):
    enabled: bool = False  # a Fase 0 (docs/SPIKE_FALANTES.md) decide o default
    latency_offset_s: float = 0.0
    min_share: float = 0.6
    min_samples: int = 3


class NotesConfig(BaseModel):
    interval_s: float = 120.0
    min_new_chars: int = 1500
    compact_every_s: float = 900.0


Level = Literal["all", "members", "admins"]


class PermissionsConfig(BaseModel):
    ask: Level = "members"
    history: Literal["admins", "members", "none"] = "admins"
    artifacts: Level = "members"


class WebConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8765
    public_base_url: str = ""  # ex.: http://servidor.lan:8765 (vazio = host:port)
    allow_edit: bool = False


class AssistantConfig(BaseModel):
    greeting: str = (
        "Olá, sou o Wendes, assistente desta reunião. Estou transcrevendo para gerar"
        " notas e a ata. Qualquer pessoa pode pausar com /pausar no chat."
    )
    speak_greeting: bool = True
    wake_word: str = ""  # vazio = room.bot_display_name
    wake_word_aliases: list[str] = ["vendes", "wendis", "uendes"]
    voice_trigger: bool = True
    voice_answers: bool = True
    stt: AssistSttConfig = AssistSttConfig()
    speakers: SpeakersConfig = SpeakersConfig()
    notes: NotesConfig = NotesConfig()
    permissions: PermissionsConfig = PermissionsConfig()
    unknown_participant: Literal["allow", "warn", "pause", "leave"] = "warn"
    web: WebConfig = WebConfig()
    # F10 (experimental): compartilhar a página como tela do bot (/tela)
    screen_share: bool = False


class ActionsConfig(BaseModel):
    lookback_days: int = 14


class DataRetentionConfig(BaseModel):
    recording_days: int = 30  # 0 = manter para sempre
    transcript_days: int = 0  # 0 = manter para sempre


class Secrets(BaseSettings):
    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", extra="ignore")
    firebase_refresh_token: str = ""
    smtp_password: str = ""
    # Chave do endpoint LLM (OpenAI e afins); vazio para vLLM/Ollama locais.
    llm_api_key: str = ""
    # Token do `wendes serve` (página do histórico)
    wendes_web_token: str = ""


class Config(BaseModel):
    room: RoomConfig
    team: TeamConfig = TeamConfig()
    meeting: MeetingConfig = MeetingConfig()
    stt: SttConfig = SttConfig()
    tts: TtsConfig = TtsConfig()
    llm: LlmConfig
    commands: CommandsConfig = CommandsConfig()
    browser: BrowserConfig = BrowserConfig()
    distribution: DistributionConfig = DistributionConfig()
    assistant: AssistantConfig = AssistantConfig()
    actions: ActionsConfig = ActionsConfig()
    data_retention: DataRetentionConfig = DataRetentionConfig()
    data_dir: str = "data"

    @property
    def secrets(self) -> Secrets:
        return Secrets()

    def resolve(self, relative: str) -> Path:
        p = Path(relative)
        return p if p.is_absolute() else PROJECT_ROOT / p


def load_config(path: Path | None = None) -> Config:
    cfg_path = path or PROJECT_ROOT / "config.yaml"
    if not cfg_path.exists():
        raise FileNotFoundError(
            f"{cfg_path} não encontrado — copie config.example.yaml para "
            "config.yaml e ajuste para a sua equipe."
        )
    with open(cfg_path, encoding="utf-8") as f:
        return Config.model_validate(yaml.safe_load(f))
