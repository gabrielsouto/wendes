#!/usr/bin/env python3
"""Fase 0 do modo assistente: mede a precisão da atribuição de falas.

Entrada: uma pasta do spike_speakers.py com recording.wav, audio_levels.jsonl e
labels.txt (faixa de rótulos do Audacity: início<TAB>fim<TAB>nome, um por linha).

Compara três abordagens sobre os segmentos do Whisper (word_timestamps=True):
  (a) heurística antiga: dominant speaker no ponto médio do segmento;
  (b) integração dos níveis de áudio no segmento, com latency_offset_s de
      -1,0 a +1,0 s em passos de 0,1 (escolhe o melhor);
  (c) a (b) com quebra do segmento por palavra quando o dominante muda ≥ 1 s.

Métricas (o rótulo "verdadeiro" de um trecho é o nome com maior sobreposição):
  precisão por tempo   = duração atribuída corretamente / duração atribuída
  cobertura por tempo  = duração atribuída / duração total dos segmentos
  precisão por segmento e cobertura por segmento, idem contando segmentos.

Uso:
  .venv/bin/python scripts/eval_speakers.py data/spike/2026-11-20_10-00 [--model medium]
  (--words words.json reaproveita uma transcrição salva; sem ela, roda o Whisper)

Regra de decisão (spec, Fase 0): se (b) ou (c) atingir ≥ 85% de precisão por
tempo, assistant.speakers.enabled: true vira o default.
"""
from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from rapidfuzz import fuzz

from wendes.assistant.speakers import (
    LevelEvent,
    Word,
    attribute_levels,
    dominant_at,
    load_levels,
    split_by_speaker,
)

THRESHOLD = 0.85


@dataclass
class Label:
    start: float
    end: float
    name: str


@dataclass
class Seg:
    start: float
    end: float
    words: list[Word]


def parse_labels(path: Path) -> list[Label]:
    out: list[Label] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) < 3 or line.startswith("\\"):  # "\" = linha de frequência do Audacity
            continue
        try:
            out.append(Label(float(parts[0]), float(parts[1]), parts[2].strip()))
        except ValueError:
            continue
    return sorted(out, key=lambda lb: lb.start)


def _norm(name: str) -> str:
    dec = unicodedata.normalize("NFKD", name.casefold())
    return " ".join("".join(c for c in dec if not unicodedata.combining(c)).split())


def same_person(a: str, b: str) -> bool:
    if not a or not b:
        return False
    return fuzz.partial_ratio(_norm(a), _norm(b)) >= 85


def truth(labels: list[Label], start: float, end: float) -> str:
    """Nome com maior sobreposição no intervalo ("" se ninguém rotulado)."""
    overlap: dict[str, float] = {}
    for lb in labels:
        ov = min(end, lb.end) - max(start, lb.start)
        if ov > 0:
            overlap[lb.name] = overlap.get(lb.name, 0.0) + ov
    if not overlap:
        return ""
    return max(overlap.items(), key=lambda kv: kv[1])[0]


def score(preds: list[tuple[float, float, str]], labels: list[Label]) -> dict:
    """preds: (início, fim, nome previsto ou "")."""
    tot_t = att_t = ok_t = 0.0
    tot_n = att_n = ok_n = 0
    for start, end, name in preds:
        dur = max(0.0, end - start)
        tot_t += dur
        tot_n += 1
        if not name:
            continue
        att_t += dur
        att_n += 1
        if same_person(name, truth(labels, start, end)):
            ok_t += dur
            ok_n += 1
    return {
        "precisao_tempo": ok_t / att_t if att_t else 0.0,
        "cobertura_tempo": att_t / tot_t if tot_t else 0.0,
        "precisao_segmento": ok_n / att_n if att_n else 0.0,
        "cobertura_segmento": att_n / tot_n if tot_n else 0.0,
    }


def shifted(events: list[LevelEvent], offset: float) -> list[LevelEvent]:
    return [LevelEvent(e.t - offset, e.pid, e.level) for e in events]


def approach_a(segs, dominant, names) -> list[tuple[float, float, str]]:
    out = []
    for s in segs:
        pid = dominant_at(dominant, (s.start + s.end) / 2)
        out.append((s.start, s.end, names.get(pid, pid)))
    return out


def approach_b(segs, events, names, min_share, min_samples):
    out = []
    for s in segs:
        pid, _ = attribute_levels(events, s.start, s.end, min_share, min_samples)
        out.append((s.start, s.end, names.get(pid, pid) if pid else ""))
    return out


def approach_c(segs, events, names, min_share, min_samples):
    out = []
    for s in segs:
        for span in split_by_speaker(s.words, events, 1.0, min_share, min_samples):
            out.append((span.start, span.end, names.get(span.pid, span.pid) if span.pid else ""))
    return out


def offsets() -> list[float]:
    return [round(-1.0 + 0.1 * i, 1) for i in range(21)]


def transcribe_words(wav: Path, model_name: str) -> list[Seg]:
    from faster_whisper import WhisperModel

    model = WhisperModel(model_name, compute_type="int8")
    segments, _ = model.transcribe(
        str(wav), language="pt", vad_filter=True, word_timestamps=True,
        condition_on_previous_text=False,
    )
    out = []
    for s in segments:
        words = [Word(w.start, w.end, w.word) for w in (s.words or [])]
        out.append(Seg(s.start, s.end, words or [Word(s.start, s.end, s.text)]))
    return out


def load_words(path: Path) -> list[Seg]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [
        Seg(s["start"], s["end"], [Word(w["start"], w["end"], w["text"]) for w in s["words"]])
        for s in data
    ]


def save_words(path: Path, segs: list[Seg]) -> None:
    path.write_text(
        json.dumps(
            [
                {"start": s.start, "end": s.end,
                 "words": [{"start": w.start, "end": w.end, "text": w.text} for w in s.words]}
                for s in segs
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def evaluate(run: Path, segs: list[Seg], min_share: float, min_samples: int) -> dict:
    labels = parse_labels(run / "labels.txt")
    events, dominant, names = load_levels(run / "audio_levels.jsonl")
    res: dict = {"a": score(approach_a(segs, dominant, names), labels), "b_offsets": {}}
    best_off, best = 0.0, None
    for off in offsets():
        sc = score(approach_b(segs, shifted(events, off), names, min_share, min_samples), labels)
        res["b_offsets"][off] = sc
        if best is None or sc["precisao_tempo"] > best["precisao_tempo"]:
            best_off, best = off, sc
    res["b"] = best
    res["offset"] = best_off
    res["c"] = score(
        approach_c(segs, shifted(events, best_off), names, min_share, min_samples), labels
    )
    res["participantes"] = sorted(set(names.values()))
    res["rotulos"] = sorted({lb.name for lb in labels})
    res["recomendacao_enabled"] = max(
        res["b"]["precisao_tempo"], res["c"]["precisao_tempo"]
    ) >= THRESHOLD
    return res


def report(res: dict) -> str:
    def row(name, sc):
        return (
            f"| {name} | {sc['precisao_tempo']:.1%} | {sc['cobertura_tempo']:.1%}"
            f" | {sc['precisao_segmento']:.1%} | {sc['cobertura_segmento']:.1%} |"
        )

    lines = [
        "| Abordagem | Precisão (tempo) | Cobertura (tempo) | Precisão (seg.) | Cobertura (seg.) |",
        "|---|---|---|---|---|",
        row("(a) dominant no ponto médio", res["a"]),
        row(f"(b) níveis, offset {res['offset']:+.1f} s", res["b"]),
        row(f"(c) níveis + quebra por palavra, offset {res['offset']:+.1f} s", res["c"]),
        "",
        "Varredura do offset (b): "
        + ", ".join(f"{o:+.1f}: {sc['precisao_tempo']:.0%}" for o, sc in res["b_offsets"].items()),
        "",
        f"Recomendação: speakers.enabled = {str(res['recomendacao_enabled']).lower()}"
        f" (limiar {THRESHOLD:.0%} de precisão por tempo em (b) ou (c));"
        f" latency_offset_s = {res['offset']:+.1f}",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("run_dir", type=Path)
    ap.add_argument("--words", type=Path, default=None, help="words.json já transcrito")
    ap.add_argument("--model", default="medium")
    ap.add_argument("--min-share", type=float, default=0.6)
    ap.add_argument("--min-samples", type=int, default=3)
    args = ap.parse_args(argv)
    run: Path = args.run_dir
    if not (run / "labels.txt").exists():
        print(f"Faltou {run / 'labels.txt'} (rótulos do Audacity)", file=sys.stderr)
        return 1
    words_path = args.words or run / "words.json"
    if words_path.exists():
        segs = load_words(words_path)
    else:
        print(f"Transcrevendo {run / 'recording.wav'} com {args.model} (word_timestamps)…")
        segs = transcribe_words(run / "recording.wav", args.model)
        save_words(run / "words.json", segs)
    res = evaluate(run, segs, args.min_share, args.min_samples)
    text = report(res)
    print(text)
    (run / "eval_speakers.md").write_text(text + "\n", encoding="utf-8")
    (run / "eval_speakers.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
