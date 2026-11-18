from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from wendes.assistant.speakers import (
    LevelEvent,
    SpeakerTracker,
    Word,
    attribute_levels,
    load_levels,
    split_by_speaker,
)

ROOT = Path(__file__).resolve().parents[1]


def _levels(pid: str, start: float, end: float, level: float = 0.5, step: float = 0.2):
    out = []
    t = start
    while t <= end + 1e-9:
        out.append(LevelEvent(round(t, 3), pid, level))
        t += step
    return out


def test_vencedor_com_energia_suficiente():
    ev = _levels("a", 0, 2) + _levels("b", 0, 2, level=0.05)
    pid, share = attribute_levels(ev, 0, 2)
    assert pid == "a"
    assert share > 0.9


def test_empate_fica_nao_identificado():
    ev = _levels("a", 0, 2) + _levels("b", 0, 2)
    pid, share = attribute_levels(ev, 0, 2, min_share=0.6)
    assert pid == ""
    assert 0.45 < share < 0.55


def test_poucas_amostras_nao_identifica():
    ev = [LevelEvent(1.0, "a", 0.9)]
    assert attribute_levels(ev, 0, 2, min_samples=3)[0] == ""
    assert attribute_levels(ev, 0, 2, min_samples=1)[0] == "a"


def test_sem_eventos_no_intervalo():
    ev = _levels("a", 10, 12)
    assert attribute_levels(ev, 0, 2) == ("", 0.0)


def test_quebra_por_palavra_quando_muda_por_mais_de_1s():
    ev = _levels("a", 0, 3) + _levels("b", 3.1, 6)
    words = [Word(i * 0.5, i * 0.5 + 0.4, f"w{i}") for i in range(12)]
    spans = split_by_speaker(words, ev, min_switch_s=1.0)
    assert [s.pid for s in spans] == ["a", "b"]
    assert spans[0].text.startswith("w0")
    assert spans[1].text.endswith("w11")


def test_troca_curta_e_absorvida():
    ev = _levels("a", 0, 6) + [LevelEvent(2.2, "b", 5.0)]
    words = [Word(i * 0.5, i * 0.5 + 0.4, f"w{i}") for i in range(12)]
    spans = split_by_speaker(words, ev, min_switch_s=1.0)
    assert len(spans) == 1


def test_tracker_usa_relogio_do_audio_com_offset_e_poda(tmp_path):
    pos = {"t": 10.0}
    tr = SpeakerTracker(
        lambda: pos["t"], latency_offset_s=0.5, log_path=tmp_path / "l.jsonl",
        name_fn=lambda n: n.upper(), keep_s=60, min_samples=1,
    )
    tr.set_name("p1", "Fulana")
    tr.on_levels([("p1", 0.7)])
    assert tr.attribute(9.4, 9.6) == ("FULANA", 1.0)
    pos["t"] = 200.0
    tr.on_levels([("p1", 0.1)])
    assert len(tr._events) == 1  # o antigo foi podado da memória
    tr.paused = True
    tr.on_levels([("p1", 0.9)])
    tr.close()
    levels, _, names = load_levels(tmp_path / "l.jsonl")
    assert [e.t for e in levels] == [9.5, 199.5]  # pausado não grava
    assert names == {"p1": "Fulana"}


def test_eval_speakers_ponta_a_ponta(tmp_path):
    path = ROOT / "scripts/eval_speakers.py"
    spec = importlib.util.spec_from_file_location("eval_speakers", path)
    ev = importlib.util.module_from_spec(spec)
    sys.modules["eval_speakers"] = ev
    spec.loader.exec_module(ev)

    # níveis gravados 0,3 s depois da fala real (latência do Jitsi)
    rows = [{"pid": "p1", "name": "Fulana"}, {"pid": "p2", "name": "Beltrano"}]
    for e in _levels("p1", 0.3, 5.3) + _levels("p2", 5.6, 10.3):
        rows.append({"t": e.t, "pid": e.pid, "level": e.level})
    rows.append({"t": 0.0, "pid": "p2", "dominant": True})  # dominant atrasado/errado
    rows.append({"t": 9.0, "pid": "p2", "dominant": True})
    (tmp_path / "audio_levels.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    (tmp_path / "labels.txt").write_text("0.0\t5.0\tFulana da Silva\n5.3\t10.0\tBeltrano\n")
    segs = [
        ev.Seg(0.0, 5.0, [Word(0.0, 5.0, "oi")]),
        ev.Seg(5.3, 10.0, [Word(5.3, 10.0, "tchau")]),
    ]
    res = ev.evaluate(tmp_path, segs, 0.6, 3)
    assert res["b"]["precisao_tempo"] == 1.0
    assert res["a"]["precisao_tempo"] < 1.0
    assert res["recomendacao_enabled"] is True
    assert "Recomendação" in ev.report(res)
