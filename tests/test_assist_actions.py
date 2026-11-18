from __future__ import annotations

import json
import sqlite3

import httpx
import pytest
from pydantic import ValidationError

from conftest import FakeLlm, FakeRoom, FakeTranscribe, energy_vad, make_cfg
from wendes.assistant.actions import (
    ActionsPayload,
    ActionStore,
    build_payload,
    load_payload,
    migrate,
    normalize_room,
    post_actions,
    save_payload,
)
from wendes.assistant.app import build_session
from wendes.assistant.finalize import finalize_assist
from wendes.assistant.notes import Notes, Op, apply_ops
from wendes.assistant.state import MeetingState
from wendes.audio.capture import AudioCapture
from wendes.facilitator.state_machine import MeetingRecord
from wendes.history.store import HistoryStore

ROOM = "https://meet.jit.si/SalaDeTeste"


def _notes(*descs):
    n = Notes()
    apply_ops(n, [Op(op="add_action", descricao=d) for d in descs])
    return n


def test_pendencias_por_janela_de_dias_e_por_sala(tmp_path):
    st = ActionStore(tmp_path / "h.sqlite")
    st.save_meeting(date="2026-11-01", started_at="09:30", room_url=ROOM, notes=_notes("velha"))
    st.save_meeting(date="2026-11-11", started_at="09:30", room_url=ROOM + "/#x",
                    notes=_notes("da semana passada"))
    st.save_meeting(date="2026-11-11", started_at="09:30", room_url=ROOM,
                    notes=_notes("regravada"))  # idempotente: troca as da mesma reunião
    st.save_meeting(date="2026-11-17", started_at="09:30", room_url="https://meet.jit.si/Outra",
                    notes=_notes("de outra sala"))
    st.save_meeting(date="2026-11-18", started_at="09:30", room_url=ROOM,
                    notes=_notes("desta reunião"))
    st.save_meeting(date="2026-11-18", started_at="08:00", room_url=ROOM,
                    notes=_notes("mais cedo hoje"))
    items = st.open_items(ROOM.upper(), date="2026-11-18", started_at="09:30", lookback_days=14)
    assert [r["descricao"] for r in items] == ["regravada", "mais cedo hoje"]
    st.resolve(items[0]["id"], "concluida", date="2026-11-18", started_at="09:30",
               evidencia="[01:40] já terminei")
    items = st.open_items(ROOM, date="2026-11-18", started_at="09:30")
    assert [r["descricao"] for r in items] == ["mais cedo hoje"]
    assert normalize_room("https://Meet.jit.si/Sala/?a=1#b") == "meet.jit.si/sala"


def test_migracao_acrescenta_colunas(tmp_path):
    db = sqlite3.connect(tmp_path / "h.sqlite")
    db.execute("CREATE TABLE action_items (id INTEGER PRIMARY KEY, descricao TEXT)")
    db.execute("INSERT INTO action_items (descricao) VALUES ('antiga')")
    db.commit()
    migrate(db)
    cols = {r[1] for r in db.execute("PRAGMA table_info(action_items)")}
    assert {"evidencia", "resolvido_em_date", "room_url", "status"} <= cols
    assert db.execute("SELECT descricao FROM action_items").fetchone() == ("antiga",)


def test_schema_do_payload(tmp_path):
    n = _notes("Reservar o auditório")
    n.acoes[0].prazo = "sexta"
    payload = build_payload(date="2026-11-18", started_at="09:30", room_url=ROOM, team="T",
                            notes=n, db_ids={"A1": 12}, evidence={})
    data = payload.model_dump()
    assert data["reuniao"] == {"data": "2026-11-18", "inicio": "09:30", "sala": ROOM,
                               "equipe": "T"}
    assert data["acoes"][0] == {"id": 12, "descricao": "Reservar o auditório",
                                "responsavel": "", "prazo": "sexta", "status": "aberta",
                                "ts": None}
    save_payload(tmp_path, payload)
    assert load_payload(tmp_path) == payload
    with pytest.raises(ValidationError):
        ActionsPayload.model_validate({"reuniao": {"data": "x"}, "acoes": []})


def test_webhook_tenta_tres_vezes_com_backoff():
    calls, sleeps = [], []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(503 if len(calls) < 3 else 200)

    payload = build_payload(date="2026-11-18", started_at="09:30", room_url=ROOM, team="",
                            notes=_notes("x"), db_ids={}, evidence={})
    ok = post_actions("http://hook/x", payload, transport=httpx.MockTransport(handler),
                      sleep=sleeps.append)
    assert ok and len(calls) == 3 and sleeps == [2.0, 4.0]
    assert calls[0]["acoes"][0]["descricao"] == "x"


def test_webhook_desiste_e_nao_insiste_em_4xx():
    calls = []

    def boom(request):
        calls.append(1)
        raise httpx.ConnectError("fora")

    payload = build_payload(date="d", started_at="h", room_url="", team="", notes=Notes(),
                            db_ids={}, evidence={})
    assert not post_actions("http://hook", payload, transport=httpx.MockTransport(boom),
                            sleep=lambda s: None)
    assert len(calls) == 3
    calls.clear()
    assert not post_actions(
        "http://hook", payload, sleep=lambda s: None,
        transport=httpx.MockTransport(lambda r: calls.append(1) or httpx.Response(400)),
    )
    assert len(calls) == 1


async def test_reuniao_2_mostra_pendencias_da_reuniao_1_e_resolve_por_fala(tmp_path,
                                                                           monkeypatch):
    import wendes.speech.stt_batch as stt_batch

    monkeypatch.setattr(stt_batch, "transcribe_recording", lambda *a, **k: [])
    cfg = make_cfg(data_dir=str(tmp_path / "data"),
                   distribution={"actions_webhook_url": "http://hook"},
                   assistant={"speak_greeting": False})
    store = HistoryStore(cfg)
    # reunião 1 (passada): duas ações abertas
    ActionStore(store.data_dir / "history.sqlite").save_meeting(
        date="2026-11-11", started_at="09:30", room_url=cfg.room.url,
        notes=_notes("Migrar o banco", "Atualizar a wiki"))

    # reunião 2
    run = store.run_dir("2026-11-18", "09:30")
    room = FakeRoom({"p1": "Fulana da Silva"})
    st = MeetingState(run, run_id="r2", date="2026-11-18", started_at="09:30",
                      room_url=cfg.room.url)
    llm = FakeLlm(routes={"NOTAS VIVAS": [json.dumps({"ops": [
        {"op": "update_action", "id": "P1", "status": "concluida", "evidencia_ts": "00:05"}]})]})
    s = build_session(cfg, room, AudioCapture(run / "recording.wav"), run, st,
                      transcribe=FakeTranscribe(), vad=energy_vad, web=False, llm=llm)
    await s.load_pending()
    assert room.sent[-1].startswith("Pendências abertas (2):\nP1. Migrar o banco (quarta,")
    assert [p["situacao"] for p in st.prev_actions] == ["sem menção", "sem menção"]
    await room.emit_chat("p1", "/pendencias")
    assert "P2. Atualizar a wiki" in room.sent[-1]

    st.add_segment(4, 6, "a migração do banco eu já terminei ontem")
    s.scheduler.start()
    s.notes.trigger()
    import asyncio

    await asyncio.sleep(0)
    await s.scheduler.join(2)
    await asyncio.gather(*s.notes._tasks)
    assert st.prev_actions[0]["situacao"] == "resolvida"
    await s.scheduler.close()

    posted = []
    import wendes.assistant.actions as actions_mod

    monkeypatch.setattr(actions_mod, "post_actions", lambda url, p, **k: posted.append(p) or True)
    rec = MeetingRecord(date="2026-11-18", started_at="09:30", mode="assist")
    finalize_assist(cfg, store, run, rec, run / "recording.wav", distribute=True, state=st)
    payload = json.loads((run / "actions_payload.json").read_text())
    assert payload["pendencias_resolvidas"][0]["descricao"] == "Migrar o banco"
    assert "já terminei" in payload["pendencias_resolvidas"][0]["evidencia"]
    assert posted and posted[0].pendencias_resolvidas[0].descricao == "Migrar o banco"
    left = ActionStore(store.data_dir / "history.sqlite").open_items(
        cfg.room.url, date="2026-11-25", started_at="09:30")
    assert [r["descricao"] for r in left] == ["Atualizar a wiki"]


async def test_cobrar_le_por_voz_e_classifica(tmp_path):
    from wendes.assistant.notes import PrevPending

    cfg = make_cfg(data_dir=str(tmp_path / "data"), assistant={"speak_greeting": False})
    room = FakeRoom({"p1": "Fulana da Silva"})
    st = MeetingState(tmp_path, run_id="r", date="2026-11-18", started_at="09:30")
    llm = FakeLlm(routes={"foi perguntada": ['{"status": "concluida"}']})
    spoken = []

    async def speak(text):
        spoken.append(text)
        st.add_segment(st.segments[-1].end + 1 if st.segments else 0, 3, "sim, já terminei")

    s = build_session(cfg, room, AudioCapture(tmp_path / "r.wav"), tmp_path, st,
                      transcribe=FakeTranscribe(), vad=energy_vad, web=False, llm=llm,
                      speak=speak)
    s.scheduler.start()
    s._set_pending([PrevPending(id="P1", descricao="Migrar o banco", responsavel="Fulana",
                                meeting_date="2026-11-11", db_id=1)])
    await room.emit_chat("p1", "/cobrar")
    assert "não tem permissão" in room.sent[-1]
    await s._cobrar(s.prev_pending, listen_s=0, settle_s=0)
    assert spoken[0] == "Fulana, a pendência Migrar o banco, da reunião de quarta, foi resolvida?"
    assert room.sent[-1] == "P1 (Migrar o banco): resolvida ✔"
    assert s.notes.notes.pendencias_anteriores[0].status == "concluida"
    await s.scheduler.close()
