from __future__ import annotations

import asyncio
import json

from conftest import FakeLlm, FakeRoom, FakeTranscribe, energy_vad, make_cfg
from wendes.assistant.app import build_session
from wendes.assistant.finalize import build_ata
from wendes.assistant.llm_queue import HUMAN, LlmScheduler
from wendes.assistant.notes import (
    CompactPoint,
    CompactResponse,
    Notes,
    NotesUpdater,
    Op,
    Point,
    PrevPending,
    Topic,
    apply_ops,
    parse_ts,
    validate_compaction,
)
from wendes.assistant.state import MeetingState
from wendes.audio.capture import AudioCapture
from wendes.facilitator.state_machine import MeetingRecord, PersonRecord


def _ops(*items):
    return [Op(**i) for i in items]


def test_aplica_operacoes_validas_e_reaproveita_topico_por_fuzzy():
    notes = Notes()
    applied, rejected = apply_ops(notes, _ops(
        {"op": "add_point", "topico": "Evento de clientes", "texto": "Local: auditório",
         "ts": "13:32"},
        {"op": "add_point", "topico": "evento de  Clientes", "texto": "Data: 12/12", "ts": 845},
        {"op": "add_decision", "texto": "Data fechada em 12/12", "ts": 845},
        {"op": "add_action", "descricao": "Reservar o auditório", "responsavel": "",
         "prazo": "sexta", "ts": 860},
        {"op": "add_open_question", "texto": "Quem cuida do coffee break?"},
    ), max_ts=900)
    assert not rejected
    assert len(applied) == 5
    assert [t.titulo for t in notes.topicos] == ["Evento de clientes"]
    assert [p.ts for p in notes.topicos[0].pontos] == [812.0, 845.0]
    assert notes.acoes[0].id == "A1" and notes.acoes[0].prazo == "sexta"
    assert notes.decisoes[0].id == "D1"


def test_operacoes_invalidas_sao_descartadas_e_nada_e_apagado():
    notes = Notes(topicos=[Topic(id="T1", titulo="Deploy", pontos=[Point(texto="Sexta 18h")])])
    applied, rejected = apply_ops(notes, _ops(
        {"op": "delete_topic", "id": "T1"},
        {"op": "add_point", "topico": "", "texto": "x"},
        {"op": "add_point", "topico": "Deploy", "texto": "Sexta 18h"},  # duplicado
        {"op": "update_action", "id": "A9", "status": "concluida"},
        {"op": "add_decision", "texto": ""},
        {"op": "add_point", "topico": "Deploy", "texto": "Rollback pronto", "ts": "99:99:99"},
    ), max_ts=100)
    assert len(applied) == 1
    assert len(rejected) == 5
    assert [p.texto for p in notes.topicos[0].pontos] == ["Sexta 18h", "Rollback pronto"]
    assert notes.topicos[0].pontos[1].ts is None  # ts fora da reunião vira None


def test_responsavel_so_quando_permitido():
    notes = Notes()
    apply_ops(notes, _ops(
        {"op": "add_action", "descricao": "Revisar o PR", "responsavel": "Fulana da Silva"},
        {"op": "add_action", "descricao": "Atualizar a wiki", "responsavel": "Beltrano"},
    ), allowed_owners={"Fulana da Silva"})
    assert notes.acoes[0].responsavel == "Fulana da Silva"
    assert notes.acoes[1].responsavel == ""


def test_update_de_acao_e_de_pendencia_anterior():
    notes = Notes(pendencias_anteriores=[PrevPending(id="P1", descricao="Migrar o banco")])
    apply_ops(notes, _ops({"op": "add_action", "descricao": "Revisar"}))
    applied, rejected = apply_ops(notes, _ops(
        {"op": "update_action", "id": "a1", "status": "Concluída"},
        {"op": "update_action", "id": "P1", "status": "resolvida", "evidencia_ts": "01:40"},
        {"op": "update_action", "id": "P1", "status": "mencionada"},
    ), max_ts=200)
    assert notes.acoes[0].status == "concluida"
    assert notes.pendencias_anteriores[0].status == "concluida"
    assert notes.pendencias_anteriores[0].evidencia_ts == 100.0
    assert len(rejected) == 1  # já resolvida não volta a "mencionada"


def test_parse_ts():
    assert parse_ts("13:32") == 812
    assert parse_ts("[1:00:05]") == 3605
    assert parse_ts(12.5) == 12.5
    assert parse_ts("abc") is None
    assert parse_ts(-1) is None
    assert parse_ts(1000, max_ts=100) is None


def test_compactacao_valida_cobertura():
    snap = [Point(texto=f"p{i}", ts=i) for i in range(4)]
    ok = CompactResponse(pontos=[
        CompactPoint(texto="p0+p2", de=[1, 3]), CompactPoint(texto="p1", de=[2]),
        CompactPoint(texto="p3", de=[4]),
    ])
    current = snap + [Point(texto="novo")]
    new = validate_compaction(snap, current, ok, None)
    assert [p.texto for p in new] == ["p0+p2", "p1", "p3", "novo"]
    assert new[0].ts == 0
    perde = CompactResponse(pontos=[CompactPoint(texto="só um", de=[1, 2])])
    assert validate_compaction(snap, current, perde, None) is None
    fora = CompactResponse(pontos=[CompactPoint(texto="x", de=[1, 2, 3, 4, 5])])
    assert validate_compaction(snap, current, fora, None) is None


def _state(tmp_path):
    st = MeetingState(tmp_path, run_id="r", date="2026-11-18", started_at="09:30")
    return st


async def test_rodada_incremental_e_persistencia(tmp_path):
    st = _state(tmp_path)
    st.add_segment(10, 14, "o evento vai ser no auditório", speaker="Fulana da Silva",
                   speaker_conf=0.9)
    st.add_segment(15, 20, "eu vou reservar o auditório até sexta", speaker="Fulana da Silva",
                   speaker_conf=0.9)
    llm = FakeLlm(routes={"NOTAS VIVAS": [json.dumps({"ops": [
        {"op": "add_point", "topico": "Evento", "texto": "No auditório", "ts": "00:10"},
        {"op": "add_action", "descricao": "Reservar o auditório", "responsavel": "Fulana da Silva",
         "prazo": "sexta", "ts": 15},
    ]})]})
    sched = LlmScheduler()
    sched.start()
    upd = NotesUpdater(st, sched, llm, system="sys", owner_names={"Fulana da Silva"})
    upd.trigger()
    upd.trigger()  # fundido: só uma rodada
    await asyncio.sleep(0)
    await sched.join(2)
    await asyncio.gather(*upd._tasks)
    assert upd.rounds == 1
    assert "<transcricao>" in llm.prompts[0] and "[00:10] Fulana da Silva:" in llm.prompts[0]
    assert upd.notes.acoes[0].responsavel == "Fulana da Silva"
    saved = json.loads((tmp_path / "notes.json").read_text())
    assert saved["_ultimo_segmento"] == 2
    assert len((tmp_path / "notes_ops.jsonl").read_text().splitlines()) == 2
    assert st.notes["acoes"][0]["descricao"] == "Reservar o auditório"
    # nada novo: não chama o LLM de novo
    assert not upd.has_new()
    # o estado é recarregado de notes.json
    again = NotesUpdater(_state(tmp_path), sched, llm, system="sys")
    assert again.last_seg_id == 2 and again.notes.topicos[0].titulo == "Evento"
    await sched.close()


async def test_json_invalido_pula_a_rodada(tmp_path):
    st = _state(tmp_path)
    st.add_segment(0, 2, "bom dia")
    llm = FakeLlm(responses=["lixo"])
    sched = LlmScheduler()
    sched.start()
    upd = NotesUpdater(st, sched, llm, system="sys")
    upd.trigger()
    await asyncio.sleep(0)
    await sched.join(2)
    await asyncio.gather(*upd._tasks)
    assert upd.failures == 1 and upd.last_seg_id == 0
    await sched.close()


async def test_notas_nao_atrasam_pergunta(tmp_path):
    st = _state(tmp_path)
    st.add_segment(0, 2, "bom dia")
    sched = LlmScheduler()
    llm = FakeLlm(responses=['{"ops": []}'])
    upd = NotesUpdater(st, sched, llm, system="sys")
    upd.trigger()
    await asyncio.sleep(0)
    q = sched.submit(HUMAN, "pergunta", lambda: "resposta")
    sched.start()
    await q
    await sched.join(2)
    assert sched.order[0] == "pergunta"
    await sched.close()


async def test_resumo_e_acoes_no_chat(tmp_path):
    cfg = make_cfg(data_dir=str(tmp_path / "d"), assistant={"speak_greeting": False})
    room = FakeRoom({"p1": "Fulana da Silva", "p9": "Visitante"})
    recap = '{"texto": "**Tópicos:** evento", "falado": "Falamos do evento."}'
    llm = FakeLlm(routes={"recap": [recap]})
    st = _state(tmp_path)
    s = build_session(cfg, room, AudioCapture(tmp_path / "r.wav"), tmp_path, st,
                      transcribe=FakeTranscribe(), vad=energy_vad, web=False, llm=llm)
    s.scheduler.start()
    apply_ops(s.notes.notes, _ops({"op": "add_action", "descricao": "Reservar", "prazo": "sexta"}))
    await room.emit_chat("p1", "/resumo")
    assert room.sent[-1] == "↳ Fulana da Silva: resumo até agora\n**Tópicos:** evento"
    await room.emit_chat("p1", "/acoes")
    assert room.sent[-1] == "Ações:\nA1. Reservar — sem responsável, prazo sexta"
    await room.emit_chat("p9", "/resumo")
    assert "não tem permissão" in room.sent[-1]
    spoken = []

    async def speak(text):
        spoken.append(text)

    s.speak_fn = speak
    await s.cmd_resumo("", "", None, False, voice=True)
    assert spoken == ["Falamos do evento."]
    await s.scheduler.close()


async def test_resumo_sem_llm_usa_as_notas(tmp_path):
    cfg = make_cfg(data_dir=str(tmp_path / "d"))
    room = FakeRoom({"p1": "Fulana da Silva"})
    build_session(cfg, room, AudioCapture(tmp_path / "r.wav"), tmp_path, _state(tmp_path),
                  transcribe=FakeTranscribe(), vad=energy_vad, web=False, llm=None)
    await room.emit_chat("p1", "/resumo")
    assert "LLM indisponível" in room.sent[-1]


def test_ata_assist_a_partir_das_notas(tmp_path):
    cfg = make_cfg(data_dir=str(tmp_path / "d"))
    notes = Notes()
    apply_ops(notes, _ops(
        {"op": "add_point", "topico": "Deploy", "texto": "Sexta | 18h", "ts": 65},
        {"op": "add_decision", "texto": "Congelar a main"},
        {"op": "add_action", "descricao": "Avisar o suporte", "prazo": "hoje"},
    ))
    notes.pendencias_anteriores = [
        PrevPending(id="P1", descricao="Migrar", meeting_date="2026-11-11", status="concluida"),
        PrevPending(id="P2", descricao="Docs", meeting_date="2026-11-11"),
    ]
    (tmp_path / "notes.json").write_text(notes.model_dump_json())
    st = MeetingState.load(tmp_path)
    st.qa = [{"pergunta": "Quando é o deploy?", "quem": "Fulana", "ts": 70,
              "resposta_curta": "Sexta às 18h.", "resposta": "Sexta às 18h [01:05]."}]
    rec = MeetingRecord(date="2026-11-18", started_at="09:30",
                        people=[PersonRecord(name="Fulana da Silva", present=True)])
    ata = build_ata(cfg, FakeLlm(responses=["Resumo do LLM."]), rec, st, room_url="sala",
                    duration_s=125)
    assert ata.startswith("# Ata da reunião — 2026-11-18")
    assert "**Duração:** 02:05" in ata and "Fulana da Silva" in ata
    assert "Resumo do LLM." in ata
    assert "- Sexta | 18h _[01:05]_" in ata
    assert "| A1 | Avisar o suporte | — | hoje | aberta |" in ata
    assert "**Resolvidas nesta reunião:**" in ata and "Migrar" in ata
    assert "**Ainda abertas:**" in ata and "Docs" in ata
    assert "**Quando é o deploy?**" in ata
    assert "interrompida" not in ata
