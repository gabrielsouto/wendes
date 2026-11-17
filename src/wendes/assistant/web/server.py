"""Página ao vivo da reunião (aiohttp + SSE), no mesmo processo e loop.

Rotas (todas exigem o token da execução):
  GET  /m/{run_id}                               página
  GET  /m/{run_id}/events                        stream SSE
  GET  /m/{run_id}/state                         estado completo (reconexão)
  POST /m/{run_id}/artifacts/{id}                edição de artefato (allow_edit)
  POST /m/{run_id}/artifacts/{id}/render-error   erro de renderização do Mermaid
  GET  /static/...                               HTML/JS/CSS da página (sem CDN)

O token (`secrets.token_urlsafe(32)`, novo a cada execução) vai na URL no
primeiro acesso (`?token=`); a resposta grava um cookie HttpOnly e os acessos
seguintes usam o cookie. Sem token válido: 403.
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
from collections.abc import Awaitable, Callable
from pathlib import Path

from aiohttp import web

from wendes.assistant.state import MeetingState

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
COOKIE = "wendes_token"
KEEPALIVE_S = 15.0

EditFn = Callable[[int, str, str], Awaitable[dict]]
RenderErrorFn = Callable[[int, int, str], Awaitable[None]]


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_middleware(token: str):
    @web.middleware
    async def check(request: web.Request, handler):
        given = request.query.get("token") or request.cookies.get(COOKIE) or ""
        header = request.headers.get("Authorization", "")
        if not given and header.startswith("Bearer "):
            given = header[len("Bearer "):]
        if not token or not secrets.compare_digest(given.encode(), token.encode()):
            return web.Response(status=403, text="403: token ausente ou inválido")
        resp = await handler(request)
        if request.query.get("token") and isinstance(resp, web.StreamResponse):
            if not resp.prepared:
                resp.set_cookie(COOKIE, token, httponly=True, samesite="Strict", path="/")
        return resp

    return check


def page_html(mode: str = "live", base: str = "/static") -> str:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    return html.replace("{{BASE}}", base).replace("{{MODE}}", mode)


class WebServer:
    def __init__(
        self,
        state: MeetingState,
        *,
        host: str = "127.0.0.1",
        port: int = 8765,
        token: str | None = None,
        public_base_url: str = "",
        allow_edit: bool = False,
        on_edit: EditFn | None = None,
        on_render_error: RenderErrorFn | None = None,
    ) -> None:
        self.state = state
        self.host = host
        self.port = port
        self.token = token or new_token()
        self.public_base_url = public_base_url.rstrip("/")
        self.allow_edit = allow_edit
        self.on_edit = on_edit
        self.on_render_error = on_render_error
        self._runner: web.AppRunner | None = None
        self._sse: set[web.StreamResponse] = set()
        self.app = self.build_app()

    # ---- app -------------------------------------------------------------
    def build_app(self) -> web.Application:
        app = web.Application(
            middlewares=[token_middleware(self.token)], client_max_size=256 * 1024
        )
        rid = self.state.run_id
        app.router.add_get(f"/m/{rid}", self.page)
        app.router.add_get(f"/m/{rid}/", self.page)
        app.router.add_get(f"/m/{rid}/state", self.get_state)
        app.router.add_get(f"/m/{rid}/events", self.events)
        app.router.add_post(f"/m/{rid}/artifacts/{{aid}}", self.edit_artifact)
        app.router.add_post(f"/m/{rid}/artifacts/{{aid}}/render-error", self.render_error)
        app.router.add_static("/static", STATIC_DIR, show_index=False)
        return app

    @property
    def base_url(self) -> str:
        return self.public_base_url or f"http://{self.host}:{self.port}"

    def page_url(self, with_token: bool = True) -> str:
        url = f"{self.base_url}/m/{self.state.run_id}"
        return f"{url}?token={self.token}" if with_token else url

    async def start(self) -> None:
        self._runner = web.AppRunner(self.app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        log.info("Página da reunião em %s (token em page_link.txt)", self.page_url(False))

    async def stop(self) -> None:
        for resp in list(self._sse):
            try:
                await resp.write_eof()
            except Exception:  # noqa: BLE001
                pass
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # ---- handlers --------------------------------------------------------
    async def page(self, request: web.Request) -> web.Response:
        mode = "live-edit" if self.allow_edit else "live"
        return web.Response(text=page_html(mode), content_type="text/html", charset="utf-8")

    async def get_state(self, request: web.Request) -> web.Response:
        snap = self.state.snapshot()
        snap["allow_edit"] = self.allow_edit
        return web.json_response(snap, dumps=lambda o: json.dumps(o, ensure_ascii=False))

    async def events(self, request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            }
        )
        if request.query.get("token"):
            resp.set_cookie(COOKIE, self.token, httponly=True, samesite="Strict", path="/")
        await resp.prepare(request)
        q = self.state.bus.subscribe()
        self._sse.add(resp)
        try:
            await resp.write(b": conectado\n\n")
            while True:
                try:
                    kind, data = await asyncio.wait_for(q.get(), KEEPALIVE_S)
                except asyncio.TimeoutError:
                    await resp.write(b": keepalive\n\n")
                    continue
                payload = json.dumps(data, ensure_ascii=False)
                await resp.write(f"event: {kind}\ndata: {payload}\n\n".encode())
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            self.state.bus.unsubscribe(q)
            self._sse.discard(resp)
        return resp

    async def edit_artifact(self, request: web.Request) -> web.Response:
        if not self.allow_edit or self.on_edit is None:
            return web.json_response({"erro": "edição desativada"}, status=403)
        try:
            aid = int(request.match_info["aid"])
            body = await request.json()
            content = str(body.get("conteudo", ""))
            author = str(body.get("autor", "")).strip()[:60] or "página"
        except (ValueError, json.JSONDecodeError):
            return web.json_response({"erro": "pedido inválido"}, status=400)
        if not content.strip():
            return web.json_response({"erro": "conteúdo vazio"}, status=400)
        try:
            result = await self.on_edit(aid, content, author)
        except KeyError:
            return web.json_response({"erro": "artefato inexistente"}, status=404)
        except ValueError as exc:
            return web.json_response({"erro": str(exc)}, status=400)
        return web.json_response(result)

    async def render_error(self, request: web.Request) -> web.Response:
        try:
            aid = int(request.match_info["aid"])
            body = await request.json()
            version = int(body.get("versao", 0))
            error = str(body.get("erro", ""))[:500]
        except (ValueError, json.JSONDecodeError):
            return web.json_response({"erro": "pedido inválido"}, status=400)
        if self.on_render_error is not None:
            await self.on_render_error(aid, version, error)
        return web.json_response({"ok": True})
