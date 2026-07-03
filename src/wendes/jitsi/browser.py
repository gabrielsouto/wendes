"""Lança o Chromium (headful, sob Xvfb) já plugado nos dispositivos virtuais."""
from __future__ import annotations

import os

from playwright.async_api import Browser, Page, async_playwright

CHROMIUM_PATH = "/usr/bin/chromium-browser"

CHROMIUM_ARGS = [
    "--use-fake-ui-for-media-stream",       # auto-aceita permissão de mic/câmera
    "--autoplay-policy=no-user-gesture-required",
    "--disable-gpu",
    "--no-sandbox",
    "--disable-dev-shm-usage",
]


class JitsiBrowser:
    def __init__(self) -> None:
        self._pw = None
        self.browser: Browser | None = None
        self.page: Page | None = None

    async def start(self) -> Page:
        env = {
            **os.environ,
            "PULSE_SOURCE": "bot_mic",   # microfone do bot = monitor do bot_mic_sink
            "PULSE_SINK": "bot_out",     # áudio da sala vai para bot_out (capturável)
        }
        self._pw = await async_playwright().start()
        self.browser = await self._pw.chromium.launch(
            executable_path=CHROMIUM_PATH,
            headless=False,  # headless "novo" tem áudio WebRTC quebrado; rodar sob Xvfb
            args=CHROMIUM_ARGS,
            env=env,
        )
        context = await self.browser.new_context(permissions=["microphone"])
        self.page = await context.new_page()
        return self.page

    async def close(self) -> None:
        if self.browser:
            await self.browser.close()
        if self._pw:
            await self._pw.stop()
