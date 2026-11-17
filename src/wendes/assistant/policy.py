"""Permissões, participante desconhecido e retenção de dados (F9).

- `members` = nome do Jitsi que bate (normalizado, sem fuzzy) com um nome ou
  alias do roster, ou admin autenticado (`AdminAuth`).
- Perguntas sobre reuniões anteriores (histórico) só pelo chat, de quem tem
  permissão, e nunca por voz: a identidade pela voz não é confiável. Com um
  participante desconhecido na sala (política `warn`), o histórico desliga.
- Retenção vale para todos os modos: apaga gravações e transcrições antigas,
  nunca `ata.md` nem `notes.json`.
"""
from __future__ import annotations

import logging
import re
import shutil
from datetime import date as Date
from datetime import timedelta
from pathlib import Path

from wendes.config import Config
from wendes.facilitator.auth import AdminAuth, normalize_name

log = logging.getLogger(__name__)

_RUN_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RECORDING_FILES = ("recording.wav",)
TRANSCRIPT_FILES = (
    "transcript.json",
    "transcript.txt",
    "live_transcript.jsonl",
    "audio_levels.jsonl",
    "qa.jsonl",
    "notes_ops.jsonl",
    "notes_live.json",
    "words.json",
)
NEVER_DELETE = ("ata.md", "notes.json")


def roster_names(cfg: Config) -> set[str]:
    names: set[str] = set()
    for m in cfg.team.members:
        names |= {normalize_name(n) for n in (m.name, *m.aliases)}
    names.discard("")
    return names


class Policy:
    def __init__(self, cfg: Config, auth: AdminAuth) -> None:
        self.cfg = cfg
        self.auth = auth
        self.perms = cfg.assistant.permissions
        self.roster = roster_names(cfg)
        self.bot_norm = normalize_name(cfg.room.bot_display_name)

    def is_admin(self, pid: str, display: str = "") -> bool:
        return self.auth.is_admin(pid, display)

    def is_member(self, pid: str, display: str = "") -> bool:
        return self.is_admin(pid, display) or normalize_name(display) in self.roster

    def is_bot(self, display: str) -> bool:
        return normalize_name(display) == self.bot_norm

    def is_unknown(self, pid: str, display: str) -> bool:
        """Fora do roster (e não é o bot nem admin autenticado)."""
        if self.is_bot(display):
            return False
        return not self.is_member(pid, display)

    def _level_ok(self, level: str, pid: str, display: str) -> bool:
        if level == "all":
            return True
        if level == "members":
            return self.is_member(pid, display)
        if level == "admins":
            return self.is_admin(pid, display)
        return False

    def can_ask(self, pid: str, display: str) -> bool:
        return self._level_ok(self.perms.ask, pid, display)

    def can_artifacts(self, pid: str, display: str) -> bool:
        return self._level_ok(self.perms.artifacts, pid, display)

    def can_history(
        self, pid: str, display: str, *, via_voice: bool, unknown_present: bool
    ) -> bool:
        if via_voice or unknown_present or self.perms.history == "none":
            return False
        return self._level_ok(self.perms.history, pid, display)

    def can_voice_ask(self) -> bool:
        """Por voz não há identidade: só vale se `ask` estiver aberto a todos
        ou a membros (quem fala está na sala, e a sala tem o aviso de entrada)."""
        return self.perms.ask in ("all", "members")


# ---- retenção ---------------------------------------------------------------
def purge(cfg: Config, dry_run: bool = False, today: Date | None = None) -> list[Path]:
    """Apaga gravações e transcrições mais antigas que o configurado.

    `data_retention.recording_days` / `transcript_days` = 0 mantêm para sempre.
    Retorna os caminhos apagados (ou que seriam apagados, em `dry_run`).
    """
    ret = cfg.data_retention
    today = today or Date.today()
    data_dir = cfg.resolve(cfg.data_dir)
    if not data_dir.exists():
        return []
    removed: list[Path] = []
    for day_dir in sorted(data_dir.iterdir()):
        if not day_dir.is_dir() or not _RUN_DATE.match(day_dir.name):
            continue
        try:
            day = Date.fromisoformat(day_dir.name)
        except ValueError:
            continue
        age = (today - day).days
        targets: list[str] = []
        if ret.recording_days > 0 and age > ret.recording_days:
            targets += RECORDING_FILES
        if ret.transcript_days > 0 and age > ret.transcript_days:
            targets += TRANSCRIPT_FILES
        if not targets:
            continue
        for run in [p for p in day_dir.iterdir() if p.is_dir()]:
            for name in targets:
                if name in NEVER_DELETE:
                    continue
                for path in (run / name, run / "replay" / name):
                    if path.exists():
                        removed.append(path)
                        if not dry_run:
                            path.unlink()
            pages = run / "page"
            if "live_transcript.jsonl" in targets and pages.exists():
                # a página exportada embute a transcrição inteira
                removed.append(pages)
                if not dry_run:
                    shutil.rmtree(pages, ignore_errors=True)
    if removed:
        log.info(
            "Retenção: %d arquivo(s) %s", len(removed),
            "seriam apagados" if dry_run else "apagados",
        )
    return removed


def cutoff(days: int, today: Date | None = None) -> str:
    return ((today or Date.today()) - timedelta(days=days)).isoformat()
