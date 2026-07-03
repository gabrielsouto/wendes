"""Retranscrição pós-reunião do WAV completo com o modelo maior."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from faster_whisper import WhisperModel

from wendes.config import Config

log = logging.getLogger(__name__)


@dataclass
class TranscriptSegment:
    start: float
    end: float
    text: str


def transcribe_recording(cfg: Config, wav_path: Path) -> list[TranscriptSegment]:
    model = WhisperModel(
        cfg.stt.batch_model,
        device="cpu",
        compute_type=cfg.stt.compute_type,
        download_root=str(cfg.resolve(cfg.stt.download_root)),
    )
    segments, info = model.transcribe(
        str(wav_path),
        language="pt",
        vad_filter=True,
        initial_prompt=cfg.stt.vocabulary or None,
    )
    out = [TranscriptSegment(s.start, s.end, s.text.strip()) for s in segments]
    log.info("Retranscrição: %d segmentos (%.1f min de áudio)", len(out), info.duration / 60)
    return out
