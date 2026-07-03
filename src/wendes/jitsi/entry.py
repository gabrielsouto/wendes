"""Entrada na sala: despacha entre moderated.jitsi.net e meet.jit.si público.

A escolha é pelo host de `room.url` no config.yaml:
  - https://moderated.jitsi.net/<Sala> — página de agendamento; bot entra
    como moderador via fluxo de wendes.jitsi.moderated.
  - https://meet.jit.si/<Sala> — sala pública direta; bot autentica com a
    mesma sessão Firebase (wendes.jitsi.public) para abrir a reunião.
"""
from __future__ import annotations

import logging
from urllib.parse import urlparse

from playwright.async_api import Page

from wendes.config import Config

log = logging.getLogger(__name__)

# Fragmento de UI aplicado à URL da conferência: sem prejoin, câmera mutada,
# mic aberto e display name do bot.
UI_FRAGMENT = (
    "config.prejoinConfig.enabled=false"
    "&config.startWithVideoMuted=true"
    "&config.startWithAudioMuted=false"
    '&userInfo.displayName="{name}"'
)


class EntryError(RuntimeError):
    """Falha ao entrar na sala (ex.: redirect para sign-in, host desconhecido)."""


def with_ui_fragment(url: str, display_name: str) -> str:
    sep = "&" if "#" in url else "#"
    return url + sep + UI_FRAGMENT.format(name=display_name)


async def enter_room(page: Page, cfg: Config, refresh_token: str) -> str:
    """Entra na sala de cfg.room.url. Retorna o link a compartilhar com a equipe."""
    host = urlparse(cfg.room.url).netloc
    if host == "moderated.jitsi.net":
        return await _enter_via_moderated(page, cfg, refresh_token)
    if host == "meet.jit.si":
        from wendes.jitsi.public import enter_public_room

        return await enter_public_room(page, cfg, refresh_token)
    raise EntryError(
        f"Host de sala não suportado: {host!r} — use moderated.jitsi.net ou meet.jit.si"
    )


async def _enter_via_moderated(page: Page, cfg: Config, refresh_token: str) -> str:
    from wendes.jitsi.auth import build_room_url, exchange_refresh_token
    from wendes.jitsi.moderated import enter_moderated_room

    jwt = None
    if refresh_token:
        jwt = exchange_refresh_token(refresh_token)
        log.info("JWT obtido — entrando como moderador")
    else:
        log.warning("Sem refresh token — tentando entrar como convidado")

    url = build_room_url(cfg.room.url, jwt, cfg.room.bot_display_name)
    await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    if "signin" in page.url or "auth-static" in page.url:
        raise EntryError(
            f"Redirecionado para sign-in ({page.url}) — token inválido/revogado"
        )
    return await enter_moderated_room(page, cfg.room.bot_display_name)
