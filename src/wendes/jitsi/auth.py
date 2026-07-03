"""Autenticação no moderated.jitsi.net via Firebase.

O sign-in interativo (Google) é feito UMA vez pelo usuário — ver
docs/OBTER_REFRESH_TOKEN.md — e o refresh token fica no .env. Aqui trocamos
o refresh token por um ID token (JWT) novo a cada execução.
"""
from __future__ import annotations

import httpx

# apiKey pública do projeto Firebase do Jitsi (meet-jit-si-66cbd), obtida de
# https://web-cdn.jitsi.net/auth-static/moderated-jitsi-net/v4/config.js
FIREBASE_API_KEY = "AIzaSyDZopz7MebzhQim1S6ghpvaetn0NtjYNdM"
TOKEN_URL = "https://securetoken.googleapis.com/v1/token"
LOOKUP_URL = "https://identitytoolkit.googleapis.com/v1/accounts:lookup"


class AuthError(RuntimeError):
    pass


def exchange_refresh_token(refresh_token: str, api_key: str = FIREBASE_API_KEY) -> str:
    """Troca o refresh token do Firebase por um ID token (JWT) válido (~1h)."""
    if not refresh_token:
        raise AuthError(
            "FIREBASE_REFRESH_TOKEN vazio no .env — ver docs/OBTER_REFRESH_TOKEN.md"
        )
    resp = httpx.post(
        TOKEN_URL,
        params={"key": api_key},
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        timeout=20,
    )
    if resp.status_code != 200:
        raise AuthError(f"Falha ao renovar token Firebase ({resp.status_code}): {resp.text[:300]}")
    return resp.json()["id_token"]


def lookup_account(id_token: str, api_key: str = FIREBASE_API_KEY) -> dict:
    """Dados da conta Firebase (uid, email, provedores) a partir do ID token."""
    resp = httpx.post(
        LOOKUP_URL, params={"key": api_key}, json={"idToken": id_token}, timeout=20
    )
    if resp.status_code != 200:
        raise AuthError(f"Falha no accounts:lookup ({resp.status_code}): {resp.text[:300]}")
    users = resp.json().get("users") or []
    if not users:
        raise AuthError("accounts:lookup não retornou usuário para o ID token")
    return users[0]


def build_room_url(room_url: str, jwt: str | None, display_name: str) -> str:
    """URL da página do moderated.jitsi.net com o JWT do Firebase.

    A entrada na conferência em si é feita depois por
    wendes.jitsi.moderated.enter_moderated_room().
    """
    if jwt:
        return f"{room_url}?jwt={jwt}"
    return room_url
