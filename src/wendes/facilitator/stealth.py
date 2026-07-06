"""Modo stealth: o bot entra mudo e não fala por voz.

No chat, envia apenas um aviso único de gravação ao entrar (em __main__);
depois só grava o áudio da sala, acompanha quem participou e registra os
eventos de dominant speaker (para atribuir falas na transcrição). A sessão
termina quando um admin envia /fim no chat (mesma lista `commands.admins` do
config, com fuzzy match do display name). Nenhuma outra resposta é enviada.
"""
from __future__ import annotations

import asyncio
import logging
import time

from rapidfuzz import fuzz

from wendes.config import Config
from wendes.jitsi.room import JitsiRoom

log = logging.getLogger(__name__)

ROSTER_POLL_SECONDS = 15


def canonical_name(cfg: Config, display: str) -> str:
    """Casa (fuzzy) um display name do Jitsi com o nome canônico da equipe."""
    display = display.strip()
    for member in cfg.team.members:
        if any(
            fuzz.partial_ratio(c.lower(), display.lower()) >= 85
            for c in (member.name, *member.aliases)
        ):
            return member.name
    return display


def is_bot_display(cfg: Config, display: str) -> bool:
    return fuzz.partial_ratio(cfg.room.bot_display_name.lower(), display.lower()) >= 90


class StealthSession:
    def __init__(self, cfg: Config, room: JitsiRoom) -> None:
        self.cfg = cfg
        self.room = room
        self.finished = asyncio.Event()
        self.attendees: dict[str, str] = {}  # participant_id -> display name
        self._t0: float | None = None
        self._speaker_events: list[tuple[float, str]] = []  # (elapsed, participant_id)
        room.on_chat_message(self.handle_chat)
        room.on_dominant_speaker(self._on_dominant_speaker)

    def mark_start(self) -> None:
        """Zera o relógio dos eventos de falante — chamar junto do início da captura."""
        self._t0 = time.monotonic()

    def _on_dominant_speaker(self, pid: str) -> None:
        if self._t0 is None:
            return
        self._speaker_events.append((time.monotonic() - self._t0, pid))

    def speaker_events(self) -> list[dict]:
        """Eventos com nomes resolvidos pelo roster: [{"t": s, "name": display}].

        Eventos do próprio bot (mudo, mas por garantia) e de pids que nunca
        apareceram no roster com nome são descartados.
        """
        out: list[dict] = []
        for t, pid in self._speaker_events:
            display = (self.attendees.get(pid) or "").strip()
            if not display or is_bot_display(self.cfg, display):
                continue
            out.append({"t": round(t, 1), "name": canonical_name(self.cfg, display)})
        return out

    def _is_admin(self, display: str) -> bool:
        return bool(display.strip()) and any(
            fuzz.partial_ratio(a.lower(), display.lower()) >= 85
            for a in self.cfg.commands.admins
        )

    async def handle_chat(self, pid: str, text: str) -> None:
        if text.strip().lower() != "/fim":
            return
        display = await self.room.get_display_name(pid)
        if not self._is_admin(display):
            log.info("/fim de não-admin ignorado (%s)", display or pid)
            return
        log.info("/fim recebido de %s — encerrando gravação", display)
        self.finished.set()

    async def _poll_roster(self) -> None:
        while not self.finished.is_set():
            try:
                for p in await self.room.list_participants():
                    if p.display_name.strip():
                        self.attendees[p.id] = p.display_name
            except Exception:  # noqa: BLE001 — roster é acessório à gravação
                log.debug("Falha ao listar participantes", exc_info=True)
            await asyncio.sleep(ROSTER_POLL_SECONDS)

    async def wait_finished(self, max_hours: float = 4.0) -> None:
        """Grava até /fim (ou o limite de segurança, para não rodar para sempre)."""
        poller = asyncio.create_task(self._poll_roster())
        try:
            await asyncio.wait_for(self.finished.wait(), timeout=max_hours * 3600)
        except asyncio.TimeoutError:
            log.warning("Limite de %.1fh atingido sem /fim — encerrando", max_hours)
        finally:
            self.finished.set()
            poller.cancel()
