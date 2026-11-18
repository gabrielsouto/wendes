from __future__ import annotations

import asyncio
import wave

import pytest

from conftest import FakeRoom, FakeTranscribe, energy_vad, make_cfg, synth_pcm
from wendes.assistant.app import build_session
from wendes.assistant.state import MeetingState
from wendes.audio.capture import CHUNK_BYTES, AudioCapture


def _cfg(tmp_path, **assistant):
    return make_cfg(
        data_dir=str(tmp_path / "data"),
        commands={"require_code": True},
        assistant={"speak_greeting": False, **assistant},
    )


async def _session(tmp_path, room=None, **assistant):
    cfg = _cfg(tmp_path, **assistant)
    room = room or FakeRoom({"p1": "Fulana da Silva", "p2": "Beltrano Souza"})
    cap = AudioCapture(tmp_path / "recording.wav")
    state = MeetingState(tmp_path, run_id="r1", date="2026-11-18", started_at="09:30")
    spoken: list[str] = []

    async def speak(text):
        spoken.append(text)

    s = build_session(
        cfg, room, cap, tmp_path, state,
        transcribe=FakeTranscribe(), vad=energy_vad, speak=speak, web=False,
        vad_in_thread=False,
    )
    s.spoken = spoken
    return s, room, cap


async def test_pausa_por_qualquer_um_grava_silencio_e_mantem_linha_do_tempo(tmp_path):
    s, room, cap = await _session(tmp_path)
    cap._wav = wave.open(str(tmp_path / "recording.wav"), "wb")
    cap._wav.setnchannels(1)
    cap._wav.setsampwidth(2)
    cap._wav.setframerate(16000)
    tone = synth_pcm([("tom", 0.5)])
    cap._ingest(tone)
    assert s.audio_q.qsize() == 1

    await room.emit_chat("p2", "/pausar")  # Beltrano não é admin: pode pausar mesmo assim
    assert s.paused and cap.paused
    assert "pausadas por Beltrano Souza" in room.sent[-1]
    cap._ingest(tone)
    cap._ingest(tone)
    assert s.audio_q.qsize() == 1  # nada novo para a transcrição
    assert cap.samples_written == 3 * CHUNK_BYTES // 2  # linha do tempo intacta

    await room.emit_chat("p2", "/retomar")
    assert s.paused  # só admin retoma
    assert "não tem permissão" in room.sent[-1]

    s.auth.grant("p1")
    await room.emit_chat("p1", "/retomar")
    assert not s.paused and not cap.paused
    marker = s.state.segments[-1]
    assert marker.marker and marker.text == "[gravação pausada por Beltrano Souza — 00:00 a 00:01]"
    cap._wav.close()
    with wave.open(str(tmp_path / "recording.wav"), "rb") as w:
        frames = w.readframes(w.getnframes())
    assert frames[CHUNK_BYTES:] == b"\x00" * (2 * CHUNK_BYTES)  # silêncio digital


async def test_link_so_em_privado_para_admin(tmp_path):
    s, room, _ = await _session(tmp_path)

    class Web:
        def page_url(self):
            return "http://x/m/r1?token=segredo"

    s.web = Web()
    await room.emit_chat("p1", "/link", private=True)
    assert room.private_sent[-1][1].startswith("Fulana da Silva: você não tem permissão")
    s.auth.grant("p1")
    await room.emit_chat("p1", "/link")
    assert "segredo" not in room.sent[-1]
    await room.emit_chat("p1", "/link", private=True)
    assert room.private_sent[-1][0] == "p1"
    assert "token=segredo" in room.private_sent[-1][1]
    assert all("segredo" not in m for m in room.sent)


async def test_fim_so_admin(tmp_path):
    s, room, _ = await _session(tmp_path)
    await room.emit_chat("p2", "/fim")
    assert not s.finished.is_set()
    s.auth.grant("p2")
    await room.emit_chat("p2", "/fim")
    assert s.finished.is_set()


async def test_ajuda_mostra_so_o_permitido(tmp_path):
    s, room, _ = await _session(tmp_path)
    room.participants["p9"] = "Visitante"
    await room.emit_chat("p9", "/ajuda")
    txt = room.sent[-1]
    assert "/pausar" in txt and "/wendes" not in txt and "/fim" not in txt
    await room.emit_chat("p2", "/ajuda")
    assert "/wendes" in room.sent[-1] and "/fim" not in room.sent[-1]


async def test_saudacao_no_chat_e_por_voz(tmp_path):
    s, room, _ = await _session(tmp_path, speak_greeting=True)
    await s.greet()
    assert room.sent[-1].startswith("Olá, sou o Wendes")
    assert s.spoken and s.state.segments[-1].source == "bot"
    assert s.state.segments[-1].speaker == "Wendes"


@pytest.mark.parametrize("mode", ["warn", "pause", "leave", "allow"])
async def test_participante_desconhecido(tmp_path, mode):
    s, room, _ = await _session(tmp_path, unknown_participant=mode)
    await room.emit_participant("join", "p3", "Fulana da Silva")
    assert not s.unknown_present
    await room.emit_participant("join", "x9", "Convidado Externo")
    if mode == "allow":
        assert room.sent == []
        return
    assert s.unknown_present
    if mode == "warn":
        assert "Convidado Externo não está na lista" in room.sent[-1]
        assert not s.paused
    elif mode == "pause":
        assert s.paused
        await room.emit_participant("leave", "x9")
        assert not s.paused  # pausa automática acaba quando a pessoa sai
        assert not s.unknown_present
    elif mode == "leave":
        assert s.finished.is_set()
        assert s.end_reason == "participante desconhecido"


async def test_bot_e_admin_autenticado_nao_sao_desconhecidos(tmp_path):
    s, room, _ = await _session(tmp_path)
    s.auth.grant("x1")
    await room.emit_participant("join", "x1", "Nome Livre")
    await room.emit_participant("join", "b", "Wendes")
    assert not s.unknown_present


async def test_segmento_ao_vivo_com_falante_quando_habilitado(tmp_path):
    s, room, cap = await _session(tmp_path, speakers={"enabled": True, "min_samples": 1})
    s.tracker.set_name("p1", "Fulana")  # alias → nome canônico
    cap.samples_written = 16000 * 2
    room.emit_levels([("p1", 0.8), ("p2", 0.05)])
    await s._on_transcribed(1.5, 2.5, "vou revisar o PR")
    seg = s.state.segments[-1]
    assert seg.speaker == "Fulana da Silva"
    assert seg.speaker_conf > 0.9
    s.tracker.close()
    assert (tmp_path / "live_transcript.jsonl").exists()
    assert (tmp_path / "audio_levels.jsonl").exists()


async def test_stop_cancela_tarefas(tmp_path):
    s, room, cap = await _session(tmp_path)
    await s.start()
    assert s._tasks
    await s.stop()
    assert s.state.status == "finished"
    assert all(t.done() for t in asyncio.all_tasks() if t.get_name() in ("transcriber", "roster"))
