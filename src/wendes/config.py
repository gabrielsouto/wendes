"""Carrega config.yaml + .env em um objeto tipado."""
from __future__ import annotations

from pathlib import Path

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


class Secrets(BaseSettings):
    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", extra="ignore")
    firebase_refresh_token: str = ""
    smtp_password: str = ""


class Config(BaseModel):
    room: RoomConfig
    team: TeamConfig = TeamConfig()
    meeting: MeetingConfig = MeetingConfig()
    stt: SttConfig = SttConfig()
    tts: TtsConfig = TtsConfig()
    llm: LlmConfig
    commands: CommandsConfig = CommandsConfig()
    distribution: DistributionConfig = DistributionConfig()
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
