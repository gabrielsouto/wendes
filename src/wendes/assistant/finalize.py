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


_env = None


def _jinja():
    global _env
    if _env is None:
        from jinja2 import Environment, FileSystemLoader

        from wendes.assistant.state import mmss

        _env = Environment(
            loader=FileSystemLoader(Path(__file__).parent / "templates"),
            trim_blocks=False,
            lstrip_blocks=False,
        )
        _env.filters["mmss"] = lambda t: mmss(float(t or 0))
        _env.filters["cell"] = lambda s: str(s).replace("|", "\\|").replace("\n", " ")
    return _env


def load_notes(run_dir: Path):
    import json

    from wendes.assistant.notes import Notes

    path = run_dir / "notes.json"
    if not path.exists():
        return Notes()
    try:
        return Notes.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except ValueError:
        log.warning("notes.json ilegível — ata sem notas")
        return Notes()


def ata_summary(cfg: Config, llm, notes) -> str:
    from wendes.assistant import prompts_ptbr as P
    from wendes.assistant.notes import full_view, recap_fallback

    lines = full_view(notes)
    if not lines:
        return "A reunião não gerou notas (pouca fala transcrita ou LLM indisponível)."
    if llm is not None:
        from wendes.assistant.budget import Part, fit

        budget = max(1000, cfg.llm.context_tokens - 1200)
        text = "\n".join(fit([Part("n", lines, budget)], budget)["n"])
        resp = llm.ask_or_none(
            P.ATA_SUMMARY.format(notas=text),
            system=P.system(cfg.room.bot_display_name, cfg.team.name),
            max_tokens=600,
            timeout=180.0,
        )
        if resp:
            return resp.strip()
    return recap_fallback(notes)


def build_ata(
    cfg: Config,
    llm,
    record: MeetingRecord,
    state: MeetingState,
    *,
    room_url: str | None,
    duration_s: float,
    page_path: Path | None = None,
) -> str:
    from wendes.assistant.state import mmss

    notes = load_notes(state.run_dir) if state.run_dir else None
    if notes is None:
        from wendes.assistant.notes import Notes

        notes = Notes()
    return _jinja().get_template("ata_assist.md.j2").render(
        date=record.date,
        started_at=record.started_at,
        duration=mmss(duration_s),
        room_url=room_url or cfg.room.url,
        attendees=[p.name for p in record.people if p.present],
        interrupted=record.interrupted,
        error=record.error,
        summary=ata_summary(cfg, llm, notes),
        notes=notes,
        artifacts=state.artifacts,
        qa=state.qa,
        page_path=str(page_path) if page_path else "",
    )


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
    from wendes.pipeline import wav_seconds

    record.mode = "assist"
    state = state or MeetingState.load(run_dir, date=record.date, started_at=record.started_at)
    duration = wav_seconds(wav_path) or (state.segments[-1].end if state.segments else 0.0)
    batch_transcript(cfg, run_dir, wav_path)
    ata = build_ata(cfg, llm, record, state, room_url=room_url, duration_s=duration)
    return _save_and_distribute(cfg, store, run_dir, record, ata, distribute)


def _save_and_distribute(cfg, store, run_dir: Path, record, ata: str, distribute: bool,
                         attachments: list[Path] | None = None) -> Path:
    from wendes.distribution.email import send_ata
    from wendes.distribution.webhook import post_ata

    if store is not None:
        from wendes.assistant.search import index_safely

        ata_path = store.save(record, ata)
        index_safely(store.data_dir, run_dir, record.date, record.started_at)
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
