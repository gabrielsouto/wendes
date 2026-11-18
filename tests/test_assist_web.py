from __future__ import annotations

import asyncio

from aiohttp.test_utils import TestClient, TestServer

from wendes.assistant.state import MeetingState
from wendes.assistant.web.server import COOKIE, WebServer


def _server(tmp_path, **kw):
    state = MeetingState(tmp_path, run_id="2026-11-18_09-30", date="2026-11-18")
    return state, WebServer(state, token="tok-123", **kw)


async def _client(srv):
    client = TestClient(TestServer(srv.app))
    await client.start_server()
    return client


async def test_sem_token_403_e_cookie_depois_do_primeiro_acesso(tmp_path):
    state, srv = _server(tmp_path)
    client = await _client(srv)
    try:
        base = "/m/2026-11-18_09-30"
        assert (await client.get(base)).status == 403
        assert (await client.get(f"{base}?token=errado")).status == 403
        assert (await client.get(f"{base}/state")).status == 403
        assert (await client.get("/static/app.js")).status == 403
        r = await client.get(f"{base}?token=tok-123")
        assert r.status == 200
        assert "HttpOnly" in r.headers["Set-Cookie"] and COOKIE in r.headers["Set-Cookie"]
        html = await r.text()
        assert 'src="/static/app.js"' in html and "http" not in html.split("<body")[0].lower()
        # cookie guardado pelo cliente vale para as próximas
        r = await client.get(f"{base}/state")
        assert r.status == 200
        assert (await r.json())["run_id"] == "2026-11-18_09-30"
        assert (await client.get("/static/app.js")).status == 200
    finally:
        await client.close()


async def test_sse_emite_eventos(tmp_path):
    state, srv = _server(tmp_path)
    client = await _client(srv)
    try:
        resp = await client.get("/m/2026-11-18_09-30/events?token=tok-123")
        assert resp.headers["Content-Type"].startswith("text/event-stream")
        assert (await resp.content.readline()).startswith(b": conectado")
        await resp.content.readline()
        for _ in range(50):
            if state.bus.subscribers:
                break
            await asyncio.sleep(0.01)
        state.add_segment(1.0, 2.0, "olá pessoal")
        state.set_status("paused", "Fulana")
        lines = []
        while len([ln for ln in lines if ln.startswith(b"data:")]) < 2:
            lines.append(await asyncio.wait_for(resp.content.readline(), 2))
        text = b"".join(lines).decode()
        assert "event: segment" in text and "olá pessoal" in text
        assert "event: status" in text and '"paused"' in text
        resp.close()
    finally:
        await client.close()


async def test_edicao_desligada_por_padrao(tmp_path):
    state, srv = _server(tmp_path)
    client = await _client(srv)
    try:
        r = await client.post(
            "/m/2026-11-18_09-30/artifacts/1?token=tok-123", json={"conteudo": "x"}
        )
        assert r.status == 403
    finally:
        await client.close()


def test_page_url_usa_public_base_url(tmp_path):
    _, srv = _server(tmp_path, public_base_url="http://servidor.lan:8765/")
    assert srv.page_url() == "http://servidor.lan:8765/m/2026-11-18_09-30?token=tok-123"
    assert srv.page_url(False) == "http://servidor.lan:8765/m/2026-11-18_09-30"
