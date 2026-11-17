from __future__ import annotations

import asyncio

from conftest import FakeTranscribe, energy_vad, synth_pcm
from wendes.assistant.transcriber import ContinuousTranscriber
from wendes.audio.capture import CHUNK_BYTES

CHUNK = CHUNK_BYTES  # 0,5 s


def _chunks(pcm: bytes):
    for i in range(0, len(pcm), CHUNK):
        yield i // 2, pcm[i : i + CHUNK]


def _make(transcribe=None, position=lambda: 0.0, **kw):
    segs: list[tuple[float, float, str]] = []
    gaps: list[tuple[float, float]] = []

    async def on_segment(start, end, text):
        segs.append((start, end, text))

    tr = ContinuousTranscriber(
        asyncio.Queue(),
        transcribe=transcribe or FakeTranscribe(),
        vad=energy_vad,
        position_fn=position,
        on_segment=on_segment,
        on_gap=lambda s, e: gaps.append((s, e)),
        vad_in_thread=False,
        **kw,
    )
    return tr, segs, gaps


def test_segmenta_por_silencio_com_tempo_do_audio():
    pcm = synth_pcm([("silencio", 1), ("tom", 2), ("silencio", 1.5), ("tom", 1), ("silencio", 2)])
    tr, _, _ = _make(silence_s=0.8)
    for start, chunk in _chunks(pcm):
        tr.feed(start, chunk, energy_vad(chunk))
    utts = list(tr.pending)
    assert [(u.start, u.end) for u in utts] == [(0.5, 3.0), (4.0, 5.5)]
    # pré-roll de um pedaço antes da fala + silêncio de fechamento
    assert len(utts[0].pcm) == 7 * CHUNK  # 0,5 + 2 s de fala + 1 s de silêncio


def test_fecha_no_maximo_de_duracao():
    pcm = synth_pcm([("tom", 7)])
    tr, _, _ = _make(max_utterance_s=3)
    for start, chunk in _chunks(pcm):
        tr.feed(start, chunk, True)
    tr.flush()
    assert [(u.start, u.end) for u in tr.pending] == [(0.0, 3.0), (3.0, 6.0), (6.0, 7.0)]


async def test_worker_transcreve_em_ordem_e_ignora_vazio():
    pcm = synth_pcm([("tom", 1), ("silencio", 1), ("tom", 1), ("silencio", 1), ("tom", 1),
                     ("silencio", 1)])
    tr, segs, _ = _make(transcribe=FakeTranscribe({0: "bom dia", 1: "", 2: "tchau"}))
    for item in _chunks(pcm):
        tr.queue.put_nowait(item)
    task = asyncio.create_task(tr.run())
    await tr.drain(timeout=5)
    task.cancel()
    assert [s[2] for s in segs] == ["bom dia", "tchau"]
    assert segs[0][:2] == (0.0, 1.0)


async def test_descarta_por_atraso_e_marca_lacuna(caplog):
    pos = {"t": 0.0}
    tr, segs, gaps = _make(position=lambda: pos["t"], max_lag_s=30)
    pcm = synth_pcm([("tom", 2), ("silencio", 1)] * 3)
    for start, chunk in _chunks(pcm):
        tr.feed(start, chunk, energy_vad(chunk))
    assert len(tr.pending) == 3
    pos["t"] = 35.0  # os dois primeiros enunciados esperam há mais de 30 s
    tr.feed(int(35.0 * 16000), synth_pcm([("silencio", 0.5)]), False)
    tr._check_lag()
    assert len(tr.pending) == 1
    assert tr.dropped == 2
    await tr._emit_gaps()
    assert gaps == [(0.0, 5.0)]
    assert "STT ao vivo atrasado" in caplog.text
    assert tr.lag == 35.0 - 6.0
