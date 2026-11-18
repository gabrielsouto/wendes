"""Fase 6: exportação estática, catch-up das notas, `wendes serve` e check."""
from __future__ import annotations

import json
import re

from aiohttp.test_utils import TestClient, TestServer

from conftest import FakeLlm, make_cfg
from wendes.assistant.checks import check_assist, port_free
from wendes.assistant.finalize import catch_up_notes, finalize_assist
from wendes.assistant.state import MeetingState, append_jsonl
from wendes.assistant.web.archive import ArchiveServer
from wendes.assistant.web.export import render_export
from wendes.facilitator.state_machine import MeetingRecord
from wendes.history.store import HistoryStore


def _state(run):
    st = MeetingState(run, run_id="r", date="2026-11-18", started_at="09:30")
    st.add_segment(0, 4, "vamos migrar o banco </script><!-- na sexta   ok")
    st.add_segment(5, 8, "decidido: Postgres 16")
    return st


def test_exportacao_autocontida_com_estado(tmp_path):
    st = _state(tmp_path)
    html = render_export(st.snapshot())
    # nada externo: nenhum <script src=...> nem stylesheet linkado
    assert not re.search(r"<script[^>]*\bsrc=", html, re.I)
    assert '<link rel="stylesheet"' not in html
    assert "__BASE__" not in html and "{{" not in html.split("<body")[0]
    # o estado vem embutido e intacto depois de decodificado
    m = re.search(r"window\.WENDES_STATE = (.*?);</script>", html, re.S)
    assert m
    snap = json.loads(m.group(1))
    assert snap["segments"][0]["text"].startswith("vamos migrar o banco </script><!--")
    # o texto do usuário não fecha a tag nem abre comentário
    assert "</script><!--" not in m.group(1)
    assert 'data-mode="export"' in html or "export" in html


def test_finalize_gera_pagina_e_cita_na_ata(tmp_path):
    cfg = make_cfg(data_dir=str(tmp_path / "data"))
    store = HistoryStore(cfg)
    run = store.run_dir("2026-11-18", "09:30")
    st = _state(run)
    rec = MeetingRecord(date="2026-11-18", started_at="09:30", mode="assist")
    ata = finalize_assist(cfg, store, run, rec, run / "recording.wav", distribute=False,
                          state=st)
    page = run / "page" / "index.html"
    assert page.exists()
    assert str(page) in ata.read_text(encoding="utf-8")
    assert "Postgres 16" in page.read_text(encoding="utf-8")


def test_catch_up_das_notas_de_sessao_que_caiu(tmp_path):
    cfg = make_cfg(data_dir=str(tmp_path / "data"))
    run = tmp_path / "run"
    run.mkdir()
    for i, text in enumerate(["vamos migrar o banco na sexta", "decidido: Postgres 16"], 1):
        append_jsonl(run / "live_transcript.jsonl",
                     {"id": i, "start": i * 5.0, "end": i * 5.0 + 4, "text": text})
    st = MeetingState.load(run, date="2026-11-18", started_at="09:30")
    llm = FakeLlm(routes={"NOTAS VIVAS": [json.dumps({"ops": [
        {"op": "add_decision", "texto": "Usar Postgres 16", "ts": "00:10"}]})]})
    rec = MeetingRecord(date="2026-11-18", started_at="09:30", mode="assist")
    assert catch_up_notes(cfg, llm, st, rec) == 1
    notes = json.loads((run / "notes.json").read_text(encoding="utf-8"))
    assert notes["decisoes"][0]["texto"] == "Usar Postgres 16"
    assert notes["_ultimo_segmento"] == 2
    assert not st.persist  # o estado carregado continua só leitura
    # nada novo: segunda chamada não pergunta de novo
    n = len(llm.prompts)
    assert catch_up_notes(cfg, llm, MeetingState.load(run), rec) == 0
    assert len(llm.prompts) == n


async def _archive(tmp_path, ask=None):
    cfg = make_cfg(data_dir=str(tmp_path / "data"))
    store = HistoryStore(cfg)
    run = store.run_dir("2026-11-18", "09:30")
    rec = MeetingRecord(date="2026-11-18", started_at="09:30", mode="assist")
    finalize_assist(cfg, store, run, rec, run / "recording.wav", distribute=False,
                    state=_state(run))
    srv = ArchiveServer(store.data_dir, "segredo", ask=ask)
    client = TestClient(TestServer(srv.app))
    await client.start_server()
    return client


async def test_serve_exige_token(tmp_path):
    client = await _archive(tmp_path)
    try:
        for path in ("/", "/m/2026-11-18/09-30", "/busca?q=banco", "/perguntar?q=x"):
            assert (await client.get(path)).status == 403, path
            assert (await client.get(path, params={"token": "x"})).status == 403
        r = await client.get("/?token=segredo")
        assert r.status == 200 and "2026-11-18" in await r.text()
        # cookie vale depois do primeiro acesso
        r = await client.get("/m/2026-11-18/09-30")
        assert r.status == 200 and "WENDES_STATE" in await r.text()
        r = await client.get("/m/2026-11-18/09-30/ata")
        assert r.status == 200 and "Ata" in await r.text()
        assert (await client.get("/m/..%2F..%2Fetc/passwd")).status == 404
        assert (await client.get("/m/2026-11-18/10-00")).status == 404
    finally:
        await client.close()


async def test_serve_busca_e_pergunta(tmp_path):
    asked = []

    def ask(q, date, hora):
        asked.append((q, date))
        return "Ficou Postgres 16.", "todo o histórico"

    client = await _archive(tmp_path, ask=ask)
    try:
        r = await client.get("/busca", params={"q": "postgres", "token": "segredo"})
        body = await r.text()
        assert r.status == 200 and "Postgres 16" in body and "/m/2026-11-18/09-30" in body
        r = await client.get("/perguntar", params={"q": "qual banco?<b>"})
        body = await r.text()
        assert "Ficou Postgres 16." in body and "<b>" not in body.split("<form")[1]
        assert asked == [("qual banco?<b>", None)]
        r = await client.get("/perguntar", params={"q": "x", "data": "ontem"})
        assert "Data inválida" in await r.text()
    finally:
        await client.close()


def test_serve_sem_token_nao_sobe(tmp_path):
    import pytest

    with pytest.raises(ValueError):
        ArchiveServer(tmp_path, "")


def test_check_assist(tmp_path):
    import socket

    cfg = make_cfg()
    results = check_assist(cfg)
    msgs = " | ".join(m for _, m in results)
    assert "aiohttp instalado" in msgs and "presentes" in msgs
    # pasta estática vazia: aponta o script de vendor
    (tmp_path / "vendor").mkdir()
    bad = check_assist(cfg, static_dir=tmp_path)
    assert any("vendor_js.sh" in m for ok, m in bad if not ok)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        assert not port_free("127.0.0.1", s.getsockname()[1])
