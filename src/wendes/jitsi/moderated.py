"""Fluxo do moderated.jitsi.net.

A URL nomeada (ex.: /MinhaSalaDaily) com o JWT do Firebase abre uma página de
agendamento, não a conferência. A sala real dos convidados é
https://meet.jit.si/moderated/<hash> (estável) e o botão "Join as moderator"
navega para ela com um JWT de moderador emitido pelo Jitsi no fragmento.
"""
from __future__ import annotations

import logging

from playwright.async_api import Page

from wendes.jitsi.entry import with_ui_fragment

log = logging.getLogger(__name__)


async def enter_moderated_room(page: Page, display_name: str) -> str:
    """Da página de agendamento, entra na conferência como moderador.

    Retorna a URL de convidado (para compartilhar com a equipe).
    """
    await page.wait_for_selector("text=Join as moderator", timeout=30_000)
    guest_url = await page.locator("input").first.input_value()
    log.info("Link de convidados da sala: %s", guest_url)

    await page.click("text=Join as moderator")
    await page.wait_for_url("**meet.jit.si/**", timeout=30_000)
    conference_url = page.url

    # Reanexa a config de UI (prejoin off, nome do bot) mantendo o #jwt=…
    final_url = with_ui_fragment(conference_url, display_name)
    # goto para about:blank força o app a reinicializar lendo o novo fragmento
    await page.goto("about:blank")
    await page.goto(final_url, wait_until="domcontentloaded", timeout=60_000)
    return guest_url
