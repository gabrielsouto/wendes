from __future__ import annotations

import json

import wendes.speech.stt_batch as stt_batch
from conftest import FakeTranscribe, energy_vad, make_cfg, synth_pcm, write_wav
from wendes.assistant.app import run_replay
from wendes.assistant.replay import TerminalRoom
from wendes.speech.stt_batch import TranscriptSegment


async def test_replay_ponta_a_ponta_com_transcricao_falsa(tmp_path, monkeypatch, capsys):
    cfg = make_cfg(data_dir=str(tmp_path / "data"), assistant={"speak_greeting": False})
    pattern = [("silencio", 0.5), ("tom", 1.5), ("silencio", 1.5), ("tom", 1.0), ("silencio", 1.5)]
    src = write_wav(tmp_path / "orig.wav", synth_pcm(pattern))
    monkeypatch.setattr(
        stt_batch, "transcribe_recording",
        lambda cfg, wav, word_timestamps=False: [TranscriptSegment(0.5, 2.0, "bom dia lote")],
    )
    out = tmp_path / "replay"
    session = await run_replay(
        cfg, src, out, speed=0,
        transcribe=FakeTranscribe(["bom dia pessoal", "vamos começar"]),
        vad=energy_vad, room=TerminalRoom(), read_stdin=False, web=False,
        commands=["/pausar", "/retomar"],
    )
    texts = [s.text for s in session.state.segments if not s.marker]
    assert texts == ["bom dia pessoal", "vamos começar"]
    live = [json.loads(ln) for ln in (out / "live_transcript.jsonl").read_text().splitlines()]
    assert [r["text"] for r in live if not r["marker"]] == texts
    assert (out / "recording.wav").exists()
    assert (out / "transcript.json").exists()
    assert (out / "ata.md").read_text(encoding="utf-8").startswith("# Ata")
    printed = capsys.readouterr().out
    assert "💬 [chat] Olá, sou o Wendes" in printed
    assert "pausadas por Operador" in printed  # o operador do terminal é admin
    assert "retomadas" in printed
