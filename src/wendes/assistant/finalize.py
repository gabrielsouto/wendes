"""Pós-reunião do modo assist (F8), chamado por `finalize_meeting(mode="assist")`.

1. Retranscrição em lote (`word_timestamps=True`) e atribuição de falas com a
   mesma linha do tempo de níveis da reunião (se houver e estiver habilitada).
2. Ata a partir das notas ao vivo (ver `build_ata`).
3. Histórico, índice de busca, ações, e-mail, webhooks e página exportada.

Cada etapa tolera a própria falha: a ata nunca se perde.
"""
from __future__ import annotations

import logging
from pathlib import Path

from wendes.assistant.state import MeetingState
from wendes.config import Config
from wendes.facilitator.state_machine import MeetingRecord
from wendes.speech.stt_batch import TranscriptSegment

log = logging.getLogger(__name__)

MIN_AUDIO_SECONDS = 1.0


def attribute_transcript(
    cfg: Config, run_dir: Path, transcript: list[TranscriptSegment]
) -> list[TranscriptSegment]:
    """Atribui falantes aos segmentos em lote, quebrando onde o dominante muda ≥ 1 s."""
    from wendes.assistant.speakers import load_levels, split_by_speaker
    from wendes.facilitator.stealth import canonical_name

    sp = cfg.assistant.speakers
    path = run_dir / "audio_levels.jsonl"
    if not sp.enabled or not path.exists():
        return transcript
    # os tempos do arquivo da sessão já têm o offset descontado
    levels, _, names = load_levels(path)
    if not levels:
        return transcript
    out: list[TranscriptSegment] = []
    for seg in transcript:
        if not seg.words:
            from wendes.assistant.speakers import attribute_levels

            pid, conf = attribute_levels(levels, seg.start, seg.end, sp.min_share, sp.min_samples)
            name = canonical_name(cfg, names.get(pid, pid)) if pid else ""
            out.append(TranscriptSegment(seg.start, seg.end, seg.text, name, round(conf, 3)))
            continue
        for span in split_by_speaker(seg.words, levels, 1.0, sp.min_share, sp.min_samples):
            name = canonical_name(cfg, names.get(span.pid, span.pid)) if span.pid else ""
            out.append(
                TranscriptSegment(span.start, span.end, span.text, name, round(span.conf, 3))
            )
    return out


def batch_transcript(cfg: Config, run_dir: Path, wav_path: Path) -> list[TranscriptSegment] | None:
    from wendes.pipeline import wav_seconds
    from wendes.speech import stt_batch

    seconds = wav_seconds(wav_path)
    if seconds <= MIN_AUDIO_SECONDS:
        log.info("Sem áudio gravado suficiente — pulando a retranscrição")
        return None
    log.info(
        "Reunião encerrada — retranscrevendo %.1f min com o modelo %s",
        seconds / 60, cfg.stt.batch_model,
    )
    try:
        transcript = stt_batch.transcribe_recording(cfg, wav_path, word_timestamps=True)
    except Exception:  # noqa: BLE001 — sem transcrição em lote, vale a ao vivo
        log.exception("Falha na retranscrição em lote — seguindo com a transcrição ao vivo")
        return None
    try:
        transcript = attribute_transcript(cfg, run_dir, transcript)
    except Exception:  # noqa: BLE001
        log.exception("Falha na atribuição de falas em lote — segue sem falantes")
    stt_batch.save_transcript(run_dir, transcript, cfg.stt.corrections)
    return transcript


def finalize_assist(
    cfg: Config,
    store,
    run_dir: Path,
    record: MeetingRecord,
    wav_path: Path,
    *,
    room_url: str | None = None,
    distribute: bool = True,
    llm=None,
    state: MeetingState | None = None,
) -> Path:
    from wendes.minutes.summarizer import generate_stealth_ata

    record.mode = "assist"
    state = state or MeetingState.load(run_dir, date=record.date, started_at=record.started_at)
    transcript = batch_transcript(cfg, run_dir, wav_path)
    ata = generate_stealth_ata(cfg, llm, record, transcript, room_url=room_url)
    return _save_and_distribute(cfg, store, run_dir, record, ata, distribute)


def _save_and_distribute(cfg, store, run_dir: Path, record, ata: str, distribute: bool,
                         attachments: list[Path] | None = None) -> Path:
    from wendes.distribution.email import send_ata
    from wendes.distribution.webhook import post_ata

    if store is not None:
        ata_path = store.save(record, ata)
    else:
        ata_path = run_dir / "ata.md"
        ata_path.write_text(ata, encoding="utf-8")
    log.info("Ata salva em %s", ata_path)
    if not distribute:
        return ata_path
    try:
        send_ata(cfg, ata, record.date)
    except Exception:  # noqa: BLE001
        log.exception("Falha ao enviar a ata por e-mail")
    try:
        post_ata(cfg, ata)
    except Exception:  # noqa: BLE001
        log.exception("Falha ao publicar a ata no webhook")
    return ata_path
