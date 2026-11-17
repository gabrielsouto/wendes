"""Retranscrição pós-reunião do WAV completo com o modelo maior."""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from wendes.config import Config

log = logging.getLogger(__name__)


@dataclass
class TranscriptSegment:
    start: float
    end: float
    text: str


def normalize_transcript_text(text: str, corrections: dict[str, str] | None = None) -> str:
    """Corrige jargões frequentes que o STT costuma confundir foneticamente."""
    for wrong, right in (corrections or {}).items():
        text = re.sub(rf"\b{re.escape(wrong)}\b", right, text, flags=re.IGNORECASE)
    return text


def stt_hotwords(cfg: Config) -> str | None:
    """Termos difíceis do config — aplicados a cada janela de áudio.

    Sem stt.hotwords no config, cai no comportamento antigo (vocabulário +
    todos os nomes/aliases), que funciona mas dilui o efeito.
    """
    if cfg.stt.hotwords:
        return ", ".join(cfg.stt.hotwords)
    terms: list[str] = []
    if cfg.stt.vocabulary:
        terms.append(cfg.stt.vocabulary)
    for m in cfg.team.members:
        terms.append(m.name)
        terms.extend(m.aliases)
    return ", ".join(terms) or None


def resolve_stt_device(cfg: Config) -> str:
    """stt.device resolvido: em "auto", cuda se o ctranslate2 enxergar uma GPU."""
    if cfg.stt.device != "auto":
        return cfg.stt.device
    try:
        import ctranslate2

        return "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
    except Exception:  # noqa: BLE001 — sem CUDA utilizável, fica na CPU
        log.debug("Falha ao consultar GPUs no ctranslate2", exc_info=True)
        return "cpu"


def stt_cpu_threads(cfg: Config) -> int:
    """stt.cpu_threads; 0 = todos os núcleos (o default do faster-whisper usa só 4)."""
    return cfg.stt.cpu_threads if cfg.stt.cpu_threads > 0 else (os.cpu_count() or 4)


def transcribe_recording(cfg: Config, wav_path: Path) -> list[TranscriptSegment]:
    from faster_whisper import WhisperModel  # import pesado: só quando transcreve

    model = WhisperModel(
        cfg.stt.batch_model,
        device=resolve_stt_device(cfg),
        compute_type=cfg.stt.compute_type,
        cpu_threads=stt_cpu_threads(cfg),
        download_root=str(cfg.resolve(cfg.stt.download_root)),
    )
    segments, info = model.transcribe(
        str(wav_path),
        language="pt",
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
        initial_prompt=cfg.stt.vocabulary or None,
        hotwords=stt_hotwords(cfg),
        beam_size=10,
        temperature=0.0,
        # Sem contexto do segmento anterior: um erro de reconhecimento não
        # contamina os seguintes nem induz loops de repetição.
        condition_on_previous_text=False,
    )
    out = [
        TranscriptSegment(
            s.start, s.end, normalize_transcript_text(s.text.strip(), cfg.stt.corrections)
        )
        for s in segments
    ]
    log.info("Retranscrição: %d segmentos (%.1f min de áudio)", len(out), info.duration / 60)
    return out


def save_transcript(
    run_dir: Path,
    segments: list[TranscriptSegment],
    corrections: dict[str, str] | None = None,
) -> None:
    """Grava transcript.json (para reprocessar sem retranscrever) e o .txt legível."""
    for seg in segments:
        seg.text = normalize_transcript_text(seg.text, corrections)
    (run_dir / "transcript.json").write_text(
        json.dumps([asdict(s) for s in segments], ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    lines = [f"[{s.start:7.1f}s] {s.text}" for s in segments]
    (run_dir / "transcript.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_transcript(
    run_dir: Path, corrections: dict[str, str] | None = None
) -> list[TranscriptSegment] | None:
    path = run_dir / "transcript.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    out = [TranscriptSegment(**s) for s in data]
    for seg in out:
        seg.text = normalize_transcript_text(seg.text, corrections)
    return out
