"""`wendes serve`: servidor persistente do histórico (Fase 6).

Rotas (todas exigem `WENDES_WEB_TOKEN`, pelo mesmo middleware da página ao vivo):
  GET  /                         lista das reuniões
  GET  /m/{data}/{HH-MM}         página exportada (page/index.html) ou a ata
  GET  /m/{data}/{HH-MM}/ata     ata em markdown (texto)
  GET  /busca?q=...              busca FTS nas transcrições e atas
  GET  /perguntar?q=...&data=    pergunta com o motor da F3 (`offline_answer`)

Só leitura: nada aqui altera o histórico.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import sqlite3
from collections.abc import Callable
from pathlib import Path

from aiohttp import web

from wendes.assistant.web.server import token_middleware

log = logging.getLogger(__name__)

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
HM_RE = re.compile(r"^\d{2}-\d{2}$")

# (pergunta, data|None, hora|None) -> (texto da resposta, escopo)
AskFn = Callable[[str, str | None, str | None], tuple[str, str]]

STYLE = """
body{font-family:system-ui,sans-serif;max-width:60rem;margin:1.5rem auto;padding:0 1rem;
line-height:1.45;color:#1d1d1f;background:#fff}
@media (prefers-color-scheme:dark){body{color:#e8e8ea;background:#16171a}a{color:#8ab4ff}}
table{border-collapse:collapse;width:100%}td,th{padding:.35rem .5rem;border-bottom:1px solid #8884;
text-align:left}form{margin:1rem 0;display:flex;gap:.5rem;flex-wrap:wrap}
input[type=text]{flex:1;min-width:12rem;padding:.4rem}pre{white-space:pre-wrap}
.hit{margin:.75rem 0;padding:.5rem .75rem;border-left:3px solid #8888}
"""


def _page(title: str, body: str) -> web.Response:
    doc = (
        "<!doctype html><html lang=pt-BR><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(title)}</title><style>{STYLE}</style></head><body>"
        f'<p><a href="/">Reuniões</a> · <a href="/busca">Busca</a> · '
        f'<a href="/perguntar">Perguntar</a></p>{body}</body></html>'
    )
    return web.Response(text=doc, content_type="text/html", charset="utf-8")


class ArchiveServer:
    def __init__(self, data_dir: Path, token: str, *, ask: AskFn | None = None) -> None:
        if not token:
            raise ValueError("WENDES_WEB_TOKEN vazio: o serve não sobe sem token")
        self.data_dir = data_dir
        self.token = token
        self.ask = ask
        self.app = web.Application(middlewares=[token_middleware(token)])
        r = self.app.router
        r.add_get("/", self.index)
        r.add_get("/m/{date}/{hm}", self.meeting)
        r.add_get("/m/{date}/{hm}/ata", self.ata)
        r.add_get("/busca", self.search)
        r.add_get("/perguntar", self.question)

    # ---- dados -----------------------------------------------------------
    def _db(self) -> sqlite3.Connection | None:
        path = self.data_dir / "history.sqlite"
        if not path.exists():
            return None
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True)

    def meetings(self) -> list[dict]:
        db = self._db()
        if db is None:
            return []
        try:
            rows = db.execute(
                "SELECT date, started_at, record_json FROM meetings"
                " ORDER BY date DESC, started_at DESC"
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        finally:
            db.close()
        out = []
        for date, started_at, rec in rows:
            try:
                mode = json.loads(rec).get("mode", "") or "daily"
            except (TypeError, ValueError):
                mode = ""
            hm = started_at.replace(":", "-")
            out.append({
                "date": date, "started_at": started_at, "mode": mode, "hm": hm,
                "page": (self.data_dir / date / hm / "page" / "index.html").exists(),
            })
        return out

    def _run_dir(self, request: web.Request) -> Path:
        date, hm = request.match_info["date"], request.match_info["hm"]
        if not DATE_RE.match(date) or not HM_RE.match(hm):
            raise web.HTTPNotFound(text="reunião inexistente")
        run = self.data_dir / date / hm
        if not run.is_dir():
            raise web.HTTPNotFound(text="reunião inexistente")
        return run

    # ---- handlers --------------------------------------------------------
    async def index(self, request: web.Request) -> web.Response:
        rows = []
        for m in self.meetings():
            link = f"/m/{m['date']}/{m['hm']}"
            label = "página" if m["page"] else "ata"
            rows.append(
                f"<tr><td>{html.escape(m['date'])}</td><td>{html.escape(m['started_at'])}</td>"
                f"<td>{html.escape(m['mode'])}</td><td><a href=\"{link}\">{label}</a>"
                f" · <a href=\"{link}/ata\">.md</a></td></tr>"
            )
        body = "<h1>Reuniões</h1>"
        if rows:
            body += ("<table><tr><th>Data</th><th>Início</th><th>Modo</th><th></th></tr>"
                     + "".join(rows) + "</table>")
        else:
            body += "<p>Nenhuma reunião no histórico.</p>"
        return _page("Reuniões", body)

    async def meeting(self, request: web.Request) -> web.StreamResponse:
        run = self._run_dir(request)
        page = run / "page" / "index.html"
        if page.exists():
            return web.Response(text=page.read_text(encoding="utf-8"),
                                content_type="text/html", charset="utf-8")
        ata = run / "ata.md"
        if not ata.exists():
            raise web.HTTPNotFound(text="reunião sem página nem ata")
        text = html.escape(ata.read_text(encoding="utf-8"))
        return _page(f"Ata {run.parent.name}", f"<pre>{text}</pre>")

    async def ata(self, request: web.Request) -> web.Response:
        ata = self._run_dir(request) / "ata.md"
        if not ata.exists():
            raise web.HTTPNotFound(text="ata inexistente")
        return web.Response(text=ata.read_text(encoding="utf-8"),
                            content_type="text/markdown", charset="utf-8")

    async def search(self, request: web.Request) -> web.Response:
        from wendes.assistant.search import search_atas, search_history

        q = request.query.get("q", "").strip()[:300]
        body = ("<h1>Busca</h1><form><input type=text name=q autofocus value=\""
                f"{html.escape(q, quote=True)}\"><button>Buscar</button></form>")
        if q:
            db = self._db()
            hits, atas = [], []
            if db is not None:
                try:
                    hits = search_history(db, q, 10)
                    atas = search_atas(db, q, 5)
                except sqlite3.OperationalError:  # histórico ainda não indexado
                    pass
                finally:
                    db.close()
            if not hits and not atas:
                body += "<p>Nada encontrado.</p>"
            for key, snip in atas:
                date, started = key.split(" ", 1)
                body += (f'<div class=hit><a href="/m/{date}/{started.replace(":", "-")}">Ata de'
                         f" {html.escape(key)}</a><br>{html.escape(snip)}</div>")
            for h in hits:
                date, started = h.meeting_key.split(" ", 1)
                lines = "<br>".join(html.escape(ln) for ln in h.lines)
                body += (f'<div class=hit><a href="/m/{date}/{started.replace(":", "-")}">'
                         f"Reunião de {html.escape(h.meeting_key)}</a><br>{lines}</div>")
        return _page("Busca", body)

    async def question(self, request: web.Request) -> web.Response:
        q = request.query.get("q", "").strip()[:500]
        date = request.query.get("data", "").strip()
        hora = request.query.get("hora", "").strip() or None
        body = ("<h1>Perguntar</h1><form><input type=text name=q autofocus value=\""
                f"{html.escape(q, quote=True)}\" placeholder=\"Sua pergunta\">"
                f"<input type=text name=data size=10 placeholder=AAAA-MM-DD value=\""
                f"{html.escape(date, quote=True)}\"><button>Perguntar</button></form>"
                "<p><small>Sem data: busca em todo o histórico.</small></p>")
        if q:
            if date and not DATE_RE.match(date):
                body += "<p>Data inválida (use AAAA-MM-DD).</p>"
            elif self.ask is None:
                body += "<p>Perguntas desativadas neste servidor.</p>"
            else:
                try:
                    text, scope = await asyncio.to_thread(self.ask, q, date or None, hora)
                except Exception as exc:  # noqa: BLE001
                    log.warning("serve: pergunta falhou (%s)", type(exc).__name__)
                    text, scope = "LLM indisponível no momento.", ""
                body += (f"<p><small>{html.escape(scope)}</small></p>"
                         f"<pre>{html.escape(text)}</pre>")
        return _page("Perguntar", body)

    async def run(self, host: str, port: int) -> None:
        runner = web.AppRunner(self.app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, host, port).start()
        log.info("wendes serve em http://%s:%d/ (use ?token=...)", host, port)
        try:
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()


def make_ask(cfg, llm) -> AskFn:
    """Pergunta numa thread: abre o próprio HistoryStore (sqlite é por thread)."""
    from wendes.assistant.notes import parse_ts
    from wendes.assistant.qa import offline_answer
    from wendes.assistant.state import mmss

    def ask(question: str, date: str | None, hora: str | None) -> tuple[str, str]:
        from wendes.history.store import HistoryStore

        store = HistoryStore(cfg)
        try:
            out, scope = offline_answer(cfg, llm, store, question, date, hora)
        finally:
            store.db.close()
        if out is None:
            return "Não consegui responder.", scope
        cites = []
        for c in out.citacoes:
            ts = parse_ts(c.ts)
            cites.append(f"{c.data or ''} {mmss(ts) if ts is not None else ''}".strip())
        text = out.resposta.strip()
        if any(cites):
            text += "\n\nCitações: " + ", ".join(c for c in cites if c)
        return text, scope

    return ask
