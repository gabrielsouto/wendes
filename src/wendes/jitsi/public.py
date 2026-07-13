"""Entrada direta em sala pública do meet.jit.si.

Desde 2023 o meet.jit.si só inicia uma reunião quando um usuário autenticado
entra (anônimos veem "Waiting for an authenticated user"). O login do site usa
o mesmo projeto Firebase do moderated.jitsi.net, então reaproveitamos o
refresh token do .env: semeamos a sessão persistida do SDK Firebase
(IndexedDB `firebaseLocalStorageDb`) antes de abrir a sala, e o bot abre a
reunião sozinho. Sem token (ou se a sessão for rejeitada), o bot segue como
convidado e aguarda um humano autenticado abrir a sala.
"""
from __future__ import annotations

import asyncio
import logging
import time

from playwright.async_api import Page

from wendes.config import Config
from wendes.jitsi.auth import FIREBASE_API_KEY, exchange_refresh_token, lookup_account
from wendes.jitsi.entry import with_ui_fragment
from wendes.jitsi.room import JOINED_JS

log = logging.getLogger(__name__)

# Botão de login do diálogo "Waiting for an authenticated user" (e variantes)
_LOGIN_SELECTORS = (
    '[data-testid="dialog.Login"]',
    'button:has-text("Log-in")',
    'button:has-text("Log in")',
    'button:has-text("Entrar")',
)

_SEED_JS = """([key, value]) => new Promise((resolve, reject) => {
    const open = indexedDB.open('firebaseLocalStorageDb', 1);
    open.onupgradeneeded = () => {
        open.result.createObjectStore('firebaseLocalStorage', { keyPath: 'fbase_key' });
    };
    open.onerror = () => reject(open.error);
    open.onsuccess = () => {
        const db = open.result;
        const tx = db.transaction('firebaseLocalStorage', 'readwrite');
        tx.objectStore('firebaseLocalStorage').put({ fbase_key: key, value });
        tx.oncomplete = () => { db.close(); resolve(true); };
        tx.onerror = () => reject(tx.error);
    };
})"""


def _persisted_user(id_token: str, refresh_token: str, account: dict) -> dict:
    """Registro de usuário no formato que o SDK Firebase JS persiste em IndexedDB."""
    now_ms = int(time.time() * 1000)
    return {
        "uid": account["localId"],
        "email": account.get("email", ""),
        "emailVerified": account.get("emailVerified", True),
        "displayName": account.get("displayName", ""),
        "isAnonymous": False,
        "photoURL": account.get("photoUrl", ""),
        "providerData": [
            {
                "providerId": p.get("providerId", ""),
                "uid": p.get("rawId", ""),
                "displayName": p.get("displayName", ""),
                "email": p.get("email", ""),
                "phoneNumber": p.get("phoneNumber"),
                "photoURL": p.get("photoUrl", ""),
            }
            for p in account.get("providerUserInfo", [])
        ],
        "stsTokenManager": {
            "refreshToken": refresh_token,
            "accessToken": id_token,
            "expirationTime": now_ms + 55 * 60 * 1000,
        },
        "createdAt": account.get("createdAt", str(now_ms)),
        "lastLoginAt": account.get("lastLoginAt", str(now_ms)),
        "apiKey": FIREBASE_API_KEY,
        "appName": "[DEFAULT]",
    }


async def seed_firebase_session(page: Page, refresh_token: str) -> None:
    """Grava a sessão Firebase do bot no IndexedDB da origem meet.jit.si."""
    id_token = exchange_refresh_token(refresh_token)
    account = lookup_account(id_token)
    user = _persisted_user(id_token, refresh_token, account)
    # IndexedDB é por origem: precisa de uma página de meet.jit.si carregada
    await page.goto("https://meet.jit.si/robots.txt", wait_until="domcontentloaded")
    key = f"firebase:authUser:{FIREBASE_API_KEY}:[DEFAULT]"
    await page.evaluate(_SEED_JS, [key, user])
    log.info("Sessão Firebase semeada como %s", user["email"] or user["uid"])


async def _resolve_auth_dialog(page: Page, timeout_s: int = 45) -> None:
    """Se a sala pedir autenticação para começar, aciona o login uma vez.

    Com a sessão semeada o signin resolve sem interação; sem ela, o bot
    apenas fica aguardando (fallback convidado) — quem espera a entrada de
    fato é o wait_joined() chamado na sequência.
    """
    clicked = False
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if await page.evaluate(JOINED_JS):
            return
        if not clicked:
            for sel in _LOGIN_SELECTORS:
                btn = page.locator(sel).first
                try:
                    if await btn.count() and await btn.is_visible():
                        log.info("Sala pede usuário autenticado — acionando login (%s)", sel)
                        await btn.click()
                        clicked = True
                        break
                except Exception:  # noqa: BLE001 — diálogo pode sumir no meio
                    continue
        await asyncio.sleep(1)
    if not clicked:
        log.info("Sem diálogo de login — aguardando a conferência abrir")


async def enter_public_room(
    page: Page, cfg: Config, refresh_token: str, room_url: str | None = None
) -> str:
    """Abre a sala pública (room_url, ou cfg.room.url); retorna a própria URL (link da equipe)."""
    room_url = room_url or cfg.room.url
    if refresh_token:
        try:
            await seed_firebase_session(page, refresh_token)
        except Exception as exc:  # noqa: BLE001
            log.warning("Falha ao semear sessão Firebase (%s) — seguindo como convidado", exc)
    else:
        log.warning(
            "Sem refresh token — como convidado, um humano logado precisa abrir a sala"
        )

    url = with_ui_fragment(room_url, cfg.room.bot_display_name)
    await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    await _resolve_auth_dialog(page)
    return room_url
