"""Comandos via chat da sala, restritos a administradores.

Quem é admin é decidido por `AdminAuth` (wendes.facilitator.auth): por padrão,
quem mandou `/auth <código>` em privado ao bot. `/permitir <nome>` delega a
permissão a outro participante presente — pelo id dele, válido para a reunião
corrente.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from rapidfuzz import fuzz

from wendes.config import Config
from wendes.facilitator.auth import AUTH_HELP, AdminAuth
from wendes.jitsi.room import JitsiRoom

log = logging.getLogger(__name__)

HELP_TEXT = (
    "Comandos do wendes (somente admins): "
    "/comece inicia a rodada · /pular (ou /proximo) pula a pessoa da vez · "
    "/encerrar encerra e gera a ata · /permitir <nome> dá permissão a outro participante · "
    "/ajuda mostra esta mensagem · " + AUTH_HELP
)


class ChatCommands:
    def __init__(self, cfg: Config, room: JitsiRoom, run_dir: Path | None = None) -> None:
        self.room = room
        self.auth = AdminAuth(cfg, room, run_dir)
        self.start = asyncio.Event()
        self.skip = asyncio.Event()
        self.end = asyncio.Event()
        room.on_chat_message(self.handle)

    def request_end(self) -> None:
        """Encerra a reunião (mesmo caminho do /encerrar; usado também pelo SIGTERM)."""
        self.end.set()
        self.start.set()  # destrava caso ainda esteja aguardando /comece

    async def handle(self, pid: str, text: str, private: bool = False) -> None:
        text = text.strip()
        if not text.startswith("/"):
            return
        cmd, _, arg = text.partition(" ")
        cmd = cmd.lower()
        arg = arg.strip()
        if cmd == "/auth":
            await self.auth.handle_auth(pid, arg, private)
            return

        display = await self.room.get_display_name(pid)
        if not self.auth.is_admin(pid, display):
            log.info("Comando de não-admin ignorado (%s): %s", display or pid, text)
            hint = f" Admins: {AUTH_HELP}." if self.auth.require_code else ""
            await self.room.send_chat(
                f"{display or 'Participante'}: você não tem permissão para comandos.{hint}"
            )
            return

        log.info("Comando de %s: %s %s", display or pid, cmd, arg)

        if cmd == "/comece":
            self.start.set()
            await self.room.send_chat("▶ Começando a rodada.")
        elif cmd in ("/pular", "/proximo", "/próximo"):
            self.skip.set()
            await self.room.send_chat("⏭ Pulando para a próxima pessoa.")
        elif cmd == "/encerrar":
            self.request_end()
            await self.room.send_chat("⏹ Encerrando a reunião e gerando a ata.")
        elif cmd == "/permitir":
            await self._grant(arg)
        elif cmd == "/ajuda":
            await self.room.send_chat(HELP_TEXT)
        else:
            await self.room.send_chat(f"Comando desconhecido: {cmd}. Use /ajuda.")

    async def _grant(self, name: str) -> None:
        if not name:
            await self.room.send_chat("Uso: /permitir <nome do participante>")
            return
        participants = await self.room.list_participants()
        best = None
        best_score = 0.0
        for p in participants:
            if not p.display_name.strip():
                continue
            score = fuzz.partial_ratio(name.lower(), p.display_name.lower())
            if score > best_score:
                best, best_score = p, score
        if best is None or best_score < 70:
            await self.room.send_chat(f"Não encontrei participante parecido com '{name}'.")
            return
        self.auth.grant(best.id)
        await self.room.send_chat(f"✔ {best.display_name} agora pode usar comandos.")
