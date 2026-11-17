"""Quem pode dar comandos no chat (run e stealth).

O nome no Jitsi é livre: qualquer um pode entrar como "Fulana da Silva".
Por isso, com `commands.require_code` ligado (padrão), só um código de 6
dígitos gerado a cada execução autentica: o admin manda `/auth <código>` por
MENSAGEM PRIVADA ao bot, e o id dele no Jitsi (estável durante a sessão)
passa a ter permissão até o fim da reunião. O código é entregue no log,
em `run_dir/admin_code.txt` (0600) e por e-mail para `commands.admin_emails`.

Com `require_code: false`, vale o nome exato (sem acentos, maiúsculas nem
espaços extras) contra `commands.admins` e os aliases desses membros em
`team.members`. Nada de casamento parcial ou fuzzy para permissão.
"""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
import unicodedata
from pathlib import Path

from wendes.config import Config

log = logging.getLogger(__name__)

MAX_FAILURES = 3
AUTH_HELP = "/auth <código> por mensagem privada ao bot autentica um admin"
LEAKED_MSG = "Código exposto no chat público — gerei um novo; use mensagem privada."


def normalize_name(name: str) -> str:
    """casefold + sem acentos + espaços colapsados."""
    decomposed = unicodedata.normalize("NFKD", name.casefold())
    no_accents = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(no_accents.split())


def _admin_names(cfg: Config) -> set[str]:
    admins = {normalize_name(a) for a in cfg.commands.admins if a.strip()}
    names = set(admins)
    for member in cfg.team.members:
        candidates = {normalize_name(c) for c in (member.name, *member.aliases)}
        if candidates & admins:
            names |= candidates
    names.discard("")
    return names


class AdminAuth:
    def __init__(self, cfg: Config, room, run_dir: Path | None = None) -> None:
        self.cfg = cfg
        self.room = room
        self.run_dir = run_dir
        self.require_code = cfg.commands.require_code
        self.admin_names = _admin_names(cfg)
        self.granted_ids: set[str] = set()
        self.failures: dict[str, int] = {}
        self.code: str | None = None
        if self.require_code:
            self._new_code()

    # ---- permissão -------------------------------------------------------
    def is_admin(self, pid: str, display: str = "") -> bool:
        if pid in self.granted_ids:
            return True
        if self.require_code:
            return False  # o nome é irrelevante: só o código autentica
        return bool(display.strip()) and normalize_name(display) in self.admin_names

    def grant(self, pid: str) -> None:
        """/permitir: delega a permissão a um participante (pelo id, nunca pelo nome)."""
        self.granted_ids.add(pid)

    # ---- código da sessão ------------------------------------------------
    def _new_code(self) -> str:
        self.code = f"{secrets.randbelow(10**6):06d}"
        return self.code

    def _write_code_file(self) -> None:
        if self.run_dir is None or self.code is None:
            return
        path = self.run_dir / "admin_code.txt"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(self.code + "\n")
        os.chmod(path, 0o600)  # se o arquivo já existia com outra permissão

    async def publish_code(self) -> None:
        """Entrega o código: log INFO, arquivo 0600 e e-mail aos admins."""
        if self.code is None:
            return
        log.info("Código de admin desta sessão: %s (envie /auth <código> em privado)", self.code)
        try:
            self._write_code_file()
        except OSError:
            log.exception("Falha ao gravar admin_code.txt")
        if self.cfg.distribution.smtp_host and self.cfg.commands.admin_emails:
            from wendes.distribution.email import send_admin_code

            try:
                await asyncio.to_thread(send_admin_code, self.cfg, self.code)
            except Exception:  # noqa: BLE001 — log e arquivo continuam valendo
                log.exception("Falha ao enviar o código de admin por e-mail")

    async def handle_auth(self, pid: str, arg: str, private: bool) -> None:
        """Trata `/auth <código>` (sempre, antes da checagem de admin)."""
        if not self.require_code:
            await self._reply(pid, private, "Autenticação por código desativada nesta sala.")
            return
        if not private:
            log.warning("/auth no chat público (de %s) — trocando o código", pid)
            self._new_code()
            await self.room.send_chat(LEAKED_MSG)
            await self.publish_code()
            return
        if self.failures.get(pid, 0) >= MAX_FAILURES:
            log.info("/auth ignorado de %s (excedeu as tentativas)", pid)
            return
        if arg.strip() and secrets.compare_digest(arg.strip(), self.code or ""):
            self.granted_ids.add(pid)
            self.failures.pop(pid, None)
            log.info("Participante %s autenticado como admin", pid)
            await self._reply(
                pid, private, "✔ Autenticado: seus comandos valem até o fim da reunião."
            )
            return
        self.failures[pid] = self.failures.get(pid, 0) + 1
        if self.failures[pid] >= MAX_FAILURES:
            log.warning(
                "%d códigos errados de %s — novas tentativas desse participante serão ignoradas",
                MAX_FAILURES, pid,
            )
        else:
            log.info("Código de admin errado de %s (%d/%d)", pid, self.failures[pid], MAX_FAILURES)
        await self._reply(pid, private, "Código inválido.")

    async def _reply(self, pid: str, private: bool, text: str) -> None:
        if private:
            await self.room.send_private(pid, text)
        else:
            await self.room.send_chat(text)
