from __future__ import annotations

import logging

from wendes.audio.capture import CHUNK_BYTES, AudioCapture


def _chunk(i: int) -> bytes:
    return bytes([i]) * 4


def _drain(q) -> list[bytes]:
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


async def test_dispatch_sem_inscritos_nao_acumula(tmp_path):
    cap = AudioCapture(tmp_path / "r.wav")
    for i in range(1000):
        cap._dispatch(_chunk(i % 256))
    assert cap._subscribers == []
    assert cap.dropped_chunks == 0


async def test_fila_cheia_mantem_os_mais_recentes_em_ordem(tmp_path):
    cap = AudioCapture(tmp_path / "r.wav")
    q = cap.subscribe(maxsize=3)
    for i in range(5):
        cap._dispatch(_chunk(i))
    assert _drain(q) == [_chunk(2), _chunk(3), _chunk(4)]
    assert cap.dropped_chunks == 2


async def test_dois_inscritos_recebem_os_mesmos_pedacos(tmp_path):
    cap = AudioCapture(tmp_path / "r.wav")
    a = cap.subscribe()
    b = cap.subscribe()
    for i in range(4):
        cap._dispatch(_chunk(i))
    expected = [_chunk(i) for i in range(4)]
    assert _drain(a) == expected
    assert _drain(b) == expected


async def test_unsubscribe_para_de_receber(tmp_path):
    cap = AudioCapture(tmp_path / "r.wav")
    q = cap.subscribe()
    cap.unsubscribe(q)
    cap._dispatch(_chunk(1))
    assert q.empty()


async def test_contador_de_descartes_e_warning_limitado(tmp_path, caplog):
    cap = AudioCapture(tmp_path / "r.wav")
    cap.subscribe(maxsize=1)
    with caplog.at_level(logging.WARNING, logger="wendes.audio.capture"):
        for i in range(11):
            cap._dispatch(_chunk(i))
    assert cap.dropped_chunks == 10
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1  # no máximo um aviso por minuto


async def test_samples_written(tmp_path):
    cap = AudioCapture(tmp_path / "r.wav")
    data = b"\x00" * CHUNK_BYTES
    cap._write(data)
    cap._write(data)
    assert cap.samples_written == 2 * CHUNK_BYTES // 2
    assert cap.samples_written == 16_000  # 2 × 0,5 s a 16 kHz
