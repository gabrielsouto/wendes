from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

import wendes.distribution.email as email_mod
from conftest import FakeLlm, FakeRoom, FakeTranscribe, energy_vad, make_cfg
from wendes.assistant.app import build_session
from wendes.assistant.artifacts import ArtifactStore, strip_fences, validate
from wendes.assistant.state import MeetingState
from wendes.audio.capture import AudioCapture

FLOW = "flowchart TD\n  A[\"Pedido\"] --> B{\"Aprovado?\"}"


def _art(tipo, titulo, conteudo):
    return json.dumps({"tipo": tipo, "titulo": titulo, "conteudo": conteudo})


@pytest.mark.parametrize(("tipo", "content", "ok"), [
    ("diagrama", f"```mermaid\n{FLOW}\n```", True),
    ("diagrama", "%% comentário\ngraph LR\nA-->B", True),
    ("diagrama", "sequenceDiagram\nA->>B: oi", True),
    ("diagrama", "Aqui está o diagrama:\nflowchart TD", False),
    ("cronograma", "gantt\ndateFormat YYYY-MM-DD", True),
    ("cronograma", FLOW, False),
    ("doc", "   ", False),
    ("doc", "```\n# Plano\n```", True),
    ("xpto", "x", False),
])
def test_validacao_e_cercas(tipo, content, ok):
    if ok:
        assert not validate(tipo, content).startswith("```")
    else:
        with pytest.raises(ValueError):
            validate(tipo, content)


def test_strip_fences_sem_fechamento():
    assert strip_fences("```mermaid\ngraph LR\nA-->B") == "graph LR\nA-->B"


def test_versoes_em_arquivo_e_indice(tmp_path):
    st = ArtifactStore(tmp_path)
    a = st.create("doc", "Plano do Evento!", "# Plano", autor="Wendes", pedido="Fulana: plano")
    st.add_version(a.id, "# Plano v2", autor="Beltrano (página)", pedido="edição")
    d = st.create("diagrama", "Fluxo", FLOW, autor="Wendes", pedido="x")
    assert (tmp_path / "artifacts/1-plano-do-evento/v1.md").read_text() == "# Plano\n"
    assert (tmp_path / "artifacts/1-plano-do-evento/v2.md").exists()
    assert (tmp_path / f"artifacts/{d.id}-fluxo/v1.mmd").exists()
    again = ArtifactStore(tmp_path)
    assert [len(x.versoes) for x in again.items] == [2, 1]
    assert again.get(1).latest.autor == "Beltrano (página)"
    assert [p.name for p in again.latest_files()] == ["v2.md", "v1.mmd"]
    assert again.find_by_title("atualiza o plano do evento com o orçamento").id == 1
    assert again.find_by_title("algo totalmente diferente") is None
    state = MeetingState.load(tmp_path)
    assert state.artifacts[0]["versoes"][1]["conteudo"] == "# Plano v2"


async def _session(tmp_path, llm, **assistant):
    cfg = make_cfg(data_dir=str(tmp_path / "data"),
                   assistant={"speak_greeting": False, **assistant})
    room = FakeRoom({"p1": "Fulana da Silva", "p9": "Visitante"})
    st = MeetingState(tmp_path, run_id="r", date="2026-11-18", started_at="09:30")
    s = build_session(cfg, room, AudioCapture(tmp_path / "r.wav"), tmp_path, st,
                      transcribe=FakeTranscribe(), vad=energy_vad, llm=llm,
                      web="web" in assistant)
    s.scheduler.start()
    st.add_segment(5, 9, "o pedido passa pelo gestor e depois pelo financeiro")
    return s, room


async def test_cria_e_atualiza_por_id_e_por_titulo(tmp_path):
    llm = FakeLlm(routes={
        "Crie": [_art("diagrama", "Fluxo de aprovação", f"```mermaid\n{FLOW}\n```")],
        "Atualize": [_art("diagrama", "Fluxo de aprovação", FLOW + "\n  B --> C[\"Financeiro\"]")],
    })
    s, room = await _session(tmp_path, llm)
    await room.emit_chat("p1", "/diagrama fluxo de aprovação que discutimos")
    assert room.sent[-1] == "📝 Criei «Fluxo de aprovação» (#1) — está na página."
    assert s.state.artifacts[0]["versoes"][0]["conteudo"] == FLOW
    await room.emit_chat("p1", "/diagrama #1 acrescenta o financeiro")
    assert room.sent[-1] == "📝 Atualizei «Fluxo de aprovação» (#1, versão 2) — está na página."
    await room.emit_chat("p1", "@wendes atualiza o fluxo de aprovação com o financeiro")
    assert "versão 3" in room.sent[-1]
    assert sum("Atualize" in p for p in llm.prompts) == 2
    await room.emit_chat("p1", "/doc #9 x")
    assert room.sent[-1] == "Não encontrei o artefato #9."
    await room.emit_chat("p9", "/doc plano")
    assert "não tem permissão" in room.sent[-1]
    await s.scheduler.close()


async def test_mermaid_invalido_tenta_de_novo_e_desiste(tmp_path):
    llm = FakeLlm(responses=[_art("diagrama", "X", "isto não é mermaid")])
    s, room = await _session(tmp_path, llm)
    await room.emit_chat("p1", "/diagrama algo")
    assert room.sent[-1].startswith("Não consegui gerar o artefato")
    assert "foi inválida" in llm.prompts[-1]
    await s.scheduler.close()


async def test_fluxo_de_reparo_uma_vez(tmp_path):
    llm = FakeLlm(routes={
        "Crie": [_art("diagrama", "Fluxo", "graph LR\nA-->B((x")],
        "não renderizou": [_art("diagrama", "Fluxo", "graph LR\nA-->B")],
    })
    s, room = await _session(tmp_path, llm)
    await room.emit_chat("p1", "/diagrama fluxo")
    await s.render_error(1, 1, "Parse error on line 2")
    await asyncio.gather(*s._requests)
    art = s.artifacts.get(1)
    assert len(art.versoes) == 2 and art.latest.reparo
    assert art.latest.autor == "Wendes (correção)"
    await s.render_error(1, 2, "de novo")  # a correção também falhou: mostra o código
    await s.render_error(1, 1, "repetido")
    assert not s._requests
    assert len(art.versoes) == 2
    assert sum("não renderizou" in p for p in llm.prompts) == 1
    await s.scheduler.close()


async def test_edicao_pela_pagina_e_preservada_pela_atualizacao(tmp_path):
    llm = FakeLlm(routes={
        "Crie": [_art("doc", "Plano", "# Plano\n- item")],
        "Atualize": [_art("doc", "Plano", "# Plano\n- item editado\n- novo")],
    })
    s, room = await _session(tmp_path, llm, web={"allow_edit": True})
    await room.emit_chat("p1", "/doc plano do evento")
    client = TestClient(TestServer(s.web.app))
    await client.start_server()
    try:
        url = f"/m/r/artifacts/1?token={s.web.token}"
        assert (await client.post("/m/r/artifacts/1", json={"conteudo": "x"})).status == 403
        r = await client.post(url, json={"conteudo": "# Plano\n- item editado", "autor": "Bel"})
        assert r.status == 200 and (await r.json())["versao"] == 2
        r = await client.post(url, json={"conteudo": "  "})
        assert r.status == 400
        r = await client.post(f"/m/r/artifacts/7?token={s.web.token}", json={"conteudo": "a"})
        assert r.status == 404
    finally:
        await client.close()
    assert s.artifacts.get(1).latest.autor == "Bel (página)"
    await room.emit_chat("p1", "/doc #1 acrescenta um item novo")
    prompt = [p for p in llm.prompts if "Atualize" in p][-1]
    assert "- item editado" in prompt  # o LLM parte da edição humana
    await s.scheduler.close()


def test_email_anexa_artefatos(tmp_path, monkeypatch):
    cfg = make_cfg(distribution={"email_enabled": True, "email_to": ["a@b"],
                                 "email_from": "w@b", "smtp_host": "x"})
    sent = []
    monkeypatch.setattr(email_mod, "_smtp_send", lambda cfg, msg: sent.append(msg))
    folder = tmp_path / "artifacts" / "2-plano"
    folder.mkdir(parents=True)
    (folder / "v3.md").write_text("# Plano")
    email_mod.send_ata(cfg, "# Ata", "2026-11-18", attachments=[folder / "v3.md"])
    names = [p.get_filename() for p in sent[0].iter_attachments()]
    assert names == ["2-plano-v3.md"]
