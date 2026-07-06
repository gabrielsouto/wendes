"""Retranscrição pós-reunião do WAV completo com o modelo maior."""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from faster_whisper import WhisperModel

from wendes.config import Config

log = logging.getLogger(__name__)


@dataclass
class TranscriptSegment:
    start: float
    end: float
    text: str
    speaker: str = ""  # vazio quando não há evento de dominant speaker


def stt_hotwords(cfg: Config) -> str | None:
    """Vocabulário + nomes/aliases da equipe — aplicado a cada janela de áudio."""
    terms: list[str] = []
    if cfg.stt.vocabulary:
        terms.append(cfg.stt.vocabulary)
    for m in cfg.team.members:
        terms.append(m.name)
        terms.extend(m.aliases)
    return ", ".join(terms) or None


def transcribe_recording(cfg: Config, wav_path: Path) -> list[TranscriptSegment]:
    model = WhisperModel(
        cfg.stt.batch_model,
        device="cpu",
        compute_type=cfg.stt.compute_type,
        cpu_threads=os.cpu_count() or 4,  # default do faster-whisper usa só 4
        download_root=str(cfg.resolve(cfg.stt.download_root)),
    )
    segments, info = model.transcribe(
        str(wav_path),
        language="pt",
        vad_filter=True,
        initial_prompt=cfg.stt.vocabulary or None,
        hotwords=stt_hotwords(cfg),
    )
    out = [TranscriptSegment(s.start, s.end, s.text.strip()) for s in segments]
    log.info("Retranscrição: %d segmentos (%.1f min de áudio)", len(out), info.duration / 60)
    return out


def save_transcript(run_dir: Path, segments: list[TranscriptSegment]) -> None:
    """Grava transcript.json (para reprocessar sem retranscrever) e o .txt legível."""
    (run_dir / "transcript.json").write_text(
        json.dumps([asdict(s) for s in segments], ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    lines = [
        f"[{s.start:7.1f}s] {s.speaker + ': ' if s.speaker else ''}{s.text}"
        for s in segments
    ]
    (run_dir / "transcript.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_transcript(run_dir: Path) -> list[TranscriptSegment] | None:
    path = run_dir / "transcript.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return [TranscriptSegment(**s) for s in data]


def assign_speakers(
    segments: list[TranscriptSegment], events: list[dict]
) -> list[TranscriptSegment]:
    """Atribui a cada segmento o dominant speaker ativo no seu ponto médio.

    `events`: lista ordenada de {"t": segundos desde o início da gravação,
    "name": nome}. Segmentos anteriores ao primeiro evento ficam sem nome.
    """
    if not events:
        return segments
    for seg in segments:
        mid = (seg.start + seg.end) / 2
        for ev in events:
            if ev["t"] <= mid:
                seg.speaker = ev.get("name", "") or seg.speaker
            else:
                break
    return segments
