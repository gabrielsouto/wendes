from __future__ import annotations

import json

import pytest

import wendes.assistant.session as session_mod
from conftest import FakeLlm, FakeRoom, FakeTranscribe, energy_vad, make_cfg
from wendes.assistant.app import build_session
from wendes.assistant.budget import estimate_tokens
from wendes.assistant.qa import (
    QAContext,
    build_prompt,
    mentions_past,
    offline_answer,
    route_keywords,
)
from wendes.assistant.search import (
    index_meeting,
    keywords,
    open_db,
    reindex_all,
    search_atas,
    search_history,
    search_segments,
)
from wendes.assistant.state import MeetingState
from wendes.audio.capture import AudioCapture
from wendes.facilitator.state_machine import MeetingRecord
from wendes.history.store import HistoryStore

ANSWER = json.dumps({
    "resposta_curta": "A Fulana vai revisar o PR.",
    "resposta": "A **Fulana** vai revisar o PR [00:10].",
    "citacoes": [{"ts": "00:10"}, {"ts": "99:00"}, {"data": "2026-11-11", "ts": 30}],
})


def test_fts_com_acentos(tmp_path):
    db = open_db(tmp_path)
    index_meeting(db, "2026-11-11 09:30", [
        (10.0, "Fulana", "vamos falar da migração do banco"),
        (20.0, "", "e da integração com o ERP"),
        (30.0, "Beltrano", "a migração fica para sexta"),
        (40.0, "", "outro assunto qualquer"),
    ], ata="# Ata\nDecidimos adiar a migração.")
    for q in ("migracao", "MIGRAÇÃO do banco", "quando é a migração?"):
        hits = search_history(db, q)
        assert hits, q
        assert hits[0].meeting_key == "2026-11-11 09:30"
        assert any("migração" in ln for ln in hits[0].lines)
    assert search_atas(db, "migracao")[0][0] == "2026-11-11 09:30"
    assert search_history(db, "de que o") == []  # só stopwords
    assert search_history(db, "migracao", exclude_key="2026-11-11 09:30") == []
    # vizinhos: dois de cada lado no mesmo bloco
    hit = search_history(db, "ERP", k=1)[0]
    assert len(hit.lines) == 4 and hit.lines[0].startswith("[00:10] Fulana:")


def test_keywords_sem_stopwords():
    assert keywords("Quem ficou de revisar o PR da migração?") == ["revisar", "migracao"]


def test_rotas_por_palavra_chave():
    assert route_keywords("cria um documento com o plano do evento") == "documento"
    assert route_keywords("desenha o fluxo de aprovação") == "diagrama"
    assert route_keywords("faz um resumo pra gente") == "resumo"
    assert route_keywords("monta um cronograma do projeto") == "cronograma"
    assert route_keywords("quem ficou de revisar o PR?") is None
    assert mentions_past("o que decidimos na reunião passada?")
    assert not mentions_past("o que decidimos hoje?")


def test_contexto_ordem_e_orcamento():
    ctx = QAContext(
        notes=["# Notas", "- ponto"],
        recent=[f"[00:{i:02d}] Fulana: frase recente {i} " + "x" * 80 for i in range(200)],
        retrieved=["[00:01] Beltrano: trecho antigo relevante"],
        history=["Reunião de 2026-11-11 09:30:", "  [00:30] ?: decidimos adiar"],
    )
    prompt = build_prompt("quem vai revisar?", "Fulana", ctx, 4096, "sys")
    assert estimate_tokens(prompt) <= 4096 - 400
    order = [prompt.index(x) for x in ("<pedido>", "<notas>", "frase recente 199",
                                       "trecho antigo", "<historico>", "2026-11-11")]
    assert order == sorted(order)
    assert "frase recente 0 " not in prompt  # a mais antiga foi cortada


async def _session(tmp_path, llm, **assistant):
    cfg = make_cfg(data_dir=str(tmp_path / "data"),
                   assistant={"speak_greeting": False, **assistant})
    room = FakeRoom({"p1": "Fulana da Silva", "p2": "Beltrano Souza", "p9": "Visitante"})
    st = MeetingState(tmp_path, run_id="r", date="2026-11-18", started_at="09:30")
    cap = AudioCapture(tmp_path / "r.wav")
    cap.samples_written = 16000 * 120
    spoken: list[str] = []

    async def speak(text):
        spoken.append(text)

    s = build_session(cfg, room, cap, tmp_path, st, transcribe=FakeTranscribe(),
                      vad=energy_vad, web=False, llm=llm, speak=speak)
    s.scheduler.start()
    s.spoken = spoken
    st.add_segment(10, 14, "eu vou revisar o PR", speaker="Fulana da Silva")
    return s, room


async def test_pergunta_no_chat_com_citacoes(tmp_path):
    llm = FakeLlm(routes={"Classifique": ['{"intencao": "pergunta"}'], "Responda à pergunta":
                          [ANSWER]})
    s, room = await _session(tmp_path, llm)
    await room.emit_chat("p2", "/wendes quem ficou de revisar o PR?")
    assert room.sent[-1] == ("↳ Beltrano Souza: quem ficou de revisar o PR?\n"
                             "A **Fulana** vai revisar o PR [00:10].")
    qa = [json.loads(x) for x in (tmp_path / "qa.jsonl").read_text().splitlines()]
    assert qa[0]["quem"] == "Beltrano Souza" and qa[0]["via"] == "chat"
    assert qa[0]["citacoes"] == [{"ts": 10.0}, {"data": "2026-11-11", "ts": 30.0}]
    prompt = [p for p in llm.prompts if "Responda à pergunta" in p][0]
    assert "<transcricao>" in prompt and "[00:10] Fulana da Silva: eu vou revisar o PR" in prompt
    assert "(não consultado)" in prompt  # Beltrano não tem permissão de histórico
    await s.scheduler.close()


async def test_pergunta_privada_recebe_resposta_privada(tmp_path):
    llm = FakeLlm(routes={"Classifique": ['{"intencao": "pergunta"}'], "Responda": [ANSWER]})
    s, room = await _session(tmp_path, llm)
    await room.emit_chat("p1", "quem revisa o PR?", private=True)
    assert room.private_sent[-1][0] == "p1"
    assert "Fulana" in room.private_sent[-1][1]
    assert not any("revisa" in m for m in room.sent)
    await s.scheduler.close()


async def test_sem_permissao_e_sem_llm(tmp_path):
    s, room = await _session(tmp_path, None)
    await room.emit_chat("p9", "/wendes oi tudo bem?")
    assert "não tem permissão" in room.sent[-1]
    await room.emit_chat("p1", "/wendes quem revisa?")
    assert "LLM indisponível" in room.sent[-1]
    await s.scheduler.close()


async def test_sinal_de_pensando_e_json_invalido(tmp_path, monkeypatch):
    monkeypatch.setattr(session_mod, "THINKING_AFTER_S", 0.01)
    llm = FakeLlm(responses=["lixo"], delay_s=0.05)
    s, room = await _session(tmp_path, llm)
    await room.emit_chat("p1", "/wendes quem revisa o PR?")
    assert "🔎 pensando…" in room.sent
    assert room.sent[-1] == "Não consegui formular a resposta agora."
    await s.scheduler.close()


@pytest.mark.parametrize(("who", "unknown", "voice", "expect_history"), [
    ("admin", False, False, True),
    ("admin", True, False, False),
    ("admin", False, True, False),
    ("member", False, False, False),
])
async def test_historico_so_com_permissao(tmp_path, who, unknown, voice, expect_history):
    db = open_db(tmp_path / "data")
    index_meeting(db, "2026-11-11 09:30", [(30.0, "", "decidimos adiar a migração do banco")])
    db.close()
    llm = FakeLlm(routes={"Classifique": ['{"intencao": "pergunta"}'], "Responda": [ANSWER]})
    s, room = await _session(tmp_path, llm)
    s.auth.grant("p1")
    if unknown:
        await room.emit_participant("join", "x", "Convidado Externo")
    pid, display = ("p1", "Fulana da Silva") if who == "admin" else ("p2", "Beltrano Souza")
    await s.ask_question("o que decidimos sobre a migração na reunião passada?",
                         pid="" if voice else pid, display=display, private=False, voice=voice)
    prompt = [p for p in llm.prompts if "Responda" in p][-1]
    assert ("decidimos adiar a migração" in prompt) is expect_history
    if not expect_history:
        assert "Não consultei reuniões anteriores" in room.sent[-1]
    await s.scheduler.close()


async def test_pergunta_por_voz_responde_falando(tmp_path):
    llm = FakeLlm(routes={"Classifique": ['{"intencao": "pergunta"}'], "Responda": [ANSWER]})
    s, room = await _session(tmp_path, llm)
    seg = s.state.add_segment(20, 23, "Wendes, quem ficou de revisar o PR?")
    await s._voice_hook(seg)
    for t in list(s._requests):
        await t
    assert s.spoken[-1] == "A Fulana vai revisar o PR."
    assert room.sent[-1].startswith("↳ alguém na sala (não identificado): quem ficou de revisar")
    assert s.state.qa[-1]["via"] == "voz"
    assert s.state.segments[-1].source == "bot"
    await s.scheduler.close()


async def test_pedido_de_resumo_por_voz_vai_para_o_resumo(tmp_path):
    llm = FakeLlm(routes={"recap": ['{"texto": "recap", "falado": "Falamos do PR."}']})
    s, room = await _session(tmp_path, llm)
    seg = s.state.add_segment(20, 23, "Wendes, faz um resumo pra gente")
    await s._voice_hook(seg)
    for t in list(s._requests):
        await t
    assert s.spoken[-1] == "Falamos do PR."
    await s.scheduler.close()


def test_search_segments_em_memoria(tmp_path):
    st = MeetingState(tmp_path, persist=False)
    for i in range(20):
        st.add_segment(i * 10, i * 10 + 5, f"assunto {i}")
    st.add_segment(300, 305, "a migração do banco fica para sexta")
    blocks = search_segments(st.segments, "migração", neighbors=1)
    assert len(blocks) == 1 and blocks[0][-1].text.startswith("a migração")


def test_wendes_ask_offline_e_reindex(tmp_path):
    cfg = make_cfg(data_dir=str(tmp_path / "data"))
    store = HistoryStore(cfg)
    rec = MeetingRecord(date="2026-11-11", started_at="09:30", mode="assist")
    store.save(rec, "# Ata\nA migração foi adiada para sexta.")
    run = store.run_dir("2026-11-11", "09:30")
    (run / "transcript.json").write_text(json.dumps([
        {"start": 30.0, "end": 33.0, "text": "a migração fica para sexta"}]))
    assert reindex_all(store.data_dir) == (1, 1)
    llm = FakeLlm(responses=[ANSWER])
    out, scope = offline_answer(cfg, llm, store, "quando é a migração?")
    assert out is not None and scope == "todo o histórico"
    assert "Reunião de 2026-11-11 09:30" in llm.prompts[-1]
    out, scope = offline_answer(cfg, llm, store, "quando é a migração?", "2026-11-11")
    assert scope == "reunião de 2026-11-11 09:30"
    assert "[00:30] não identificado: a migração fica para sexta" in llm.prompts[-1]
    out, scope = offline_answer(cfg, llm, store, "x", "2020-01-01")
    assert out is None
