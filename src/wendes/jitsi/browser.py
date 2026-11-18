"""Lança o Chromium (headful, sob Xvfb) já plugado nos dispositivos virtuais."""
from __future__ import annotations

import logging
import os
from pathlib import Path

from playwright.async_api import Browser, Page, async_playwright

from wendes.config import Config

log = logging.getLogger(__name__)

# Chromium do sistema, usado quando browser.executable_path está vazio e ele existe
CHROMIUM_PATH = "/usr/bin/chromium-browser"

CHROMIUM_ARGS = [
    "--use-fake-ui-for-media-stream",       # auto-aceita permissão de mic/câmera
    "--autoplay-policy=no-user-gesture-required",
    "--disable-gpu",
    "--no-sandbox",
    "--disable-dev-shm-usage",
]


def resolve_executable(cfg: Config) -> str | None:
    """Executável do Chromium; None = o que vem com o Playwright."""
    configured = cfg.browser.executable_path.strip()
    if configured:
        path = Path(configured).expanduser()
        if not path.exists():
            raise FileNotFoundError(
                f"browser.executable_path aponta para {path}, que não existe — corrija o"
                " config.yaml ou deixe vazio para usar o Chromium do Playwright"
            )
        return str(path)
    if Path(CHROMIUM_PATH).exists():
        log.info("Usando o Chromium do sistema: %s", CHROMIUM_PATH)
        return CHROMIUM_PATH
    log.info("Usando o Chromium do Playwright (playwright install chromium)")
    return None


class JitsiBrowser:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._pw = None
        self.browser: Browser | None = None
        self.page: Page | None = None

    async def start(self, headless: bool = False, extra_args: list[str] | None = None) -> Page:
        env = {
            **os.environ,
            "PULSE_SOURCE": "bot_mic",   # microfone do bot = monitor do bot_mic_sink
            "PULSE_SINK": "bot_out",     # áudio da sala vai para bot_out (capturável)
        }
        executable = resolve_executable(self.cfg)
        extra = {"executable_path": executable} if executable else {}
        self._pw = await async_playwright().start()
        self.browser = await self._pw.chromium.launch(
            # headless "novo" tem áudio WebRTC quebrado; na reunião, rodar sob Xvfb
            headless=headless,
            args=CHROMIUM_ARGS + list(extra_args or []),
            env=env,
            **extra,
        )
        context = await self.browser.new_context(permissions=["microphone"])
        self.page = await context.new_page()
        return self.page

    async def check(self) -> str:
        """Lança em headless só para validar o executável; retorna a versão."""
        await self.start(headless=True)
        try:
            assert self.browser is not None
            return self.browser.version
        finally:
            await self.close()

    async def close(self) -> None:
        if self.browser:
            await self.browser.close()
        if self._pw:
            await self._pw.stop()
