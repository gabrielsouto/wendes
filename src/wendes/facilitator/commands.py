"""Comandos via chat da sala, restritos a administradores.

Admins iniciais vêm de `commands.admins` no config (fuzzy match com o display
name). `/permitir <nome>` delega a permissão a outro participante presente —
válido para a reunião corrente (o nome entra na lista de admins da sessão).
"""
from __future__ import annotations

import asyncio
import logging

from rapidfuzz import fuzz

from wendes.config import Config
from wendes.jitsi.room import JitsiRoom

log = logging.getLogger(__name__)

HELP_TEXT = (
    "Comandos do wendes (somente admins): "
    "/comece inicia a rodada · /pular (ou /proximo) pula a pessoa da vez · "
    "/encerrar encerra e gera a ata · /permitir <nome> dá permissão a outro participante · "
    "/ajuda mostra esta mensagem"
)


class ChatCommands:
    def __init__(self, cfg: Config, room: JitsiRoom) -> None:
        self.room = room
        self.admin_names: list[str] = list(cfg.commands.admins)
        self.granted_ids: set[str] = set()
        self.start = asyncio.Event()
        self.skip = asyncio.Event()
        self.end = asyncio.Event()
        room.on_chat_message(self.handle)

    def _is_admin(self, pid: str, display: str) -> bool:
        if pid in self.granted_ids:
            return True
        return any(
            fuzz.partial_ratio(a.lower(), display.lower()) >= 85
            for a in self.admin_names
            if display.strip()
        )

    async def handle(self, pid: str, text: str) -> None:
        text = text.strip()
        if not text.startswith("/"):
            return
        display = await self.room.get_display_name(pid)
        if not self._is_admin(pid, display):
            log.info("Comando de não-admin ignorado (%s): %s", display or pid, text)
            await self.room.send_chat(
                f"{display or 'Participante'}: você não tem permissão para comandos."
            )
            return

        cmd, _, arg = text.partition(" ")
        cmd = cmd.lower()
        arg = arg.strip()
        log.info("Comando de %s: %s %s", display or pid, cmd, arg)

        if cmd == "/comece":
            self.start.set()
            await self.room.send_chat("▶ Começando a rodada.")
        elif cmd in ("/pular", "/proximo", "/próximo"):
            self.skip.set()
            await self.room.send_chat("⏭ Pulando para a próxima pessoa.")
        elif cmd == "/encerrar":
            self.end.set()
            self.start.set()  # destrava caso ainda esteja aguardando /comece
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
        self.granted_ids.add(best.id)
        self.admin_names.append(best.display_name)
        await self.room.send_chat(f"✔ {best.display_name} agora pode usar comandos.")
