"""F10 (experimental): compartilhar a página ao vivo como tela do bot no Jitsi.

Caminho (a validar no meet.jit.si, ver README):
- o Chromium é lançado com flags que escolhem sozinhas a fonte de captura pelo
  título (`screen_share_args`), já que não há ninguém para clicar no seletor;
- uma segunda página no mesmo navegador abre a página da reunião com um título
  fixo (`SCREEN_TITLE`, travado por init script);
- na página do Jitsi, `APP.conference.toggleScreenSharing(true)`.

Tudo atrás de `assistant.screen_share` (padrão: desligado).
"""
from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)

SCREEN_TITLE = "Wendes - pagina da reuniao"

# trava o título: a página ao vivo não pode trocá-lo, senão a seleção automática falha
LOCK_TITLE_JS = f"""
(() => {{
  const T = {SCREEN_TITLE!r};
  const fix = () => {{ if (document.title !== T) document.title = T; }};
  document.addEventListener('DOMContentLoaded', () => {{
    fix();
    new MutationObserver(fix).observe(document.head || document.documentElement,
      {{subtree: true, childList: true, characterData: true}});
  }});
}})();
"""

SHARING_JS = "() => !!(APP.conference.isSharingScreen)"
TOGGLE_JS = "(on) => APP.conference.toggleScreenSharing(on)"


def screen_share_args(title: str = SCREEN_TITLE) -> list[str]:
    """Flags do Chromium para escolher a fonte de captura sem diálogo."""
    return [
        f"--auto-select-desktop-capture-source={title}",
        f"--auto-select-tab-capture-source-by-title={title}",
        "--enable-usermedia-screen-capturing",
        "--allow-http-screen-capture",
    ]


class ScreenShare:
    """Abre a página numa segunda aba e liga/desliga o compartilhamento."""

    def __init__(self, jitsi_page, *, settle_s: float = 2.0, verify_s: float = 8.0) -> None:
        self.jitsi_page = jitsi_page
        self.settle_s = settle_s
        self.verify_s = verify_s
        self.page = None

    @property
    def active(self) -> bool:
        return self.page is not None

    async def start(self, url: str) -> bool:
        """True se o Jitsi confirmou o compartilhamento."""
        if self.page is None:
            self.page = await self.jitsi_page.context.new_page()
            await self.page.add_init_script(LOCK_TITLE_JS)
            await self.page.set_viewport_size({"width": 1920, "height": 1080})
            await self.page.goto(url, wait_until="domcontentloaded")
            await asyncio.sleep(self.settle_s)  # SSE e mermaid renderizam
        await self.jitsi_page.bring_to_front()
        await self.jitsi_page.evaluate(TOGGLE_JS, True)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.verify_s
        while loop.time() < deadline:
            if await self._sharing():
                log.info("Compartilhamento de tela ativo")
                return True
            await asyncio.sleep(0.5)
        log.warning("O Jitsi não confirmou o compartilhamento de tela")
        await self.stop()
        return False

    async def _sharing(self) -> bool:
        try:
            return bool(await self.jitsi_page.evaluate(SHARING_JS))
        except Exception:  # noqa: BLE001
            return False

    async def stop(self) -> None:
        try:
            if await self._sharing():
                await self.jitsi_page.evaluate(TOGGLE_JS, False)
        except Exception:  # noqa: BLE001
            log.warning("Falha ao desligar o compartilhamento", exc_info=True)
        if self.page is not None:
            try:
                await self.page.close()
            except Exception:  # noqa: BLE001
                pass
            self.page = None
