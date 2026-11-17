"""Pós-reunião comum aos modos run e stealth, e encerramento ordenado por sinal.

`finalize_meeting` roda sempre que houve reunião — inclusive quando ela falhou
no meio ou o serviço recebeu SIGTERM —, para que a ata nunca se perca.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import wave
from collections.abc import Callable
from pathlib import Path

from wendes.config import Config
from wendes.facilitator.state_machine import MeetingRecord
from wendes.history.store import HistoryStore

log = logging.getLogger(__name__)

MIN_AUDIO_SECONDS = 1.0
_WAV_HEADER_BYTES = 44
_BYTES_PER_SECOND = 16_000 * 2  # 16 kHz mono s16le


def wav_seconds(wav_path: Path) -> float:
    """Duração do WAV; se o cabeçalho não fechou (processo morto), estima pelo tamanho."""
    if not wav_path.exists():
        return 0.0
    try:
        with wave.open(str(wav_path), "rb") as w:
            frames, rate = w.getnframes(), w.getframerate()
        if frames and rate:
            return frames / rate
    except (wave.Error, EOFError, OSError):
        pass
    return max(0, wav_path.stat().st_size - _WAV_HEADER_BYTES) / _BYTES_PER_SECOND


def _make_llm(cfg: Config):
    from wendes.llm.client import Llm

    try:
        return Llm(cfg)
    except Exception:  # noqa: BLE001 — sem LLM a ata sai pelo resumo básico
        log.warning("LLM indisponível — a ata sai com o resumo básico", exc_info=True)
        return None


def finalize_meeting(
    cfg: Config,
    store: HistoryStore,
    run_dir: Path,
    record: MeetingRecord,
    wav_path: Path,
    *,
    mode: str,
    room_url: str | None = None,
) -> Path:
    """Retranscreve, gera a ata, salva no histórico e distribui. Retorna o ata.md."""
    from wendes.distribution.email import send_ata
    from wendes.distribution.webhook import post_ata
    from wendes.minutes.summarizer import generate_ata, generate_stealth_ata
    from wendes.speech.stt_batch import save_transcript, transcribe_recording

    record.mode = record.mode or mode
    transcript = None
    seconds = wav_seconds(wav_path)
    if seconds > MIN_AUDIO_SECONDS:
        log.info(
            "Reunião encerrada — retranscrevendo %.1f min com o modelo %s",
            seconds / 60, cfg.stt.batch_model,
        )
        try:
            transcript = transcribe_recording(cfg, wav_path)
        except Exception:  # noqa: BLE001 — sem transcrição ainda sai a ata básica
            log.exception("Falha na retranscrição em lote — seguindo sem transcrição")
        if transcript:
            save_transcript(run_dir, transcript, cfg.stt.corrections)
    else:
        log.info("Sem áudio gravado suficiente — pulando a retranscrição")

    llm = _make_llm(cfg)
    if mode == "stealth":
        ata = generate_stealth_ata(cfg, llm, record, transcript, room_url=room_url)
    else:
        ata = generate_ata(cfg, llm, record, transcript)
    ata_path = store.save(record, ata)
    log.info("Ata salva em %s", ata_path)

    try:
        send_ata(cfg, ata, record.date)
    except Exception:  # noqa: BLE001 — um canal não derruba o outro
        log.exception("Falha ao enviar a ata por e-mail")
    try:
        post_ata(cfg, ata)
    except Exception:  # noqa: BLE001
        log.exception("Falha ao publicar a ata no webhook")
    return ata_path


class GracefulStop:
    """SIGTERM/SIGINT encerram a reunião de forma ordenada (com ata).

    Antes de entrar na sala, o primeiro sinal cancela a tarefa principal.
    Depois, aciona o callback registrado com `on_meeting_stop` (o mesmo caminho
    do /encerrar ou do /fim). Um segundo sinal força a saída imediata.
    """

    SIGNALS = (signal.SIGTERM, signal.SIGINT)

    def __init__(self, task: asyncio.Task | None = None) -> None:
        self.task = task
        self.requested = False
        self._callback: Callable[[], None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def install(self) -> GracefulStop:
        self._loop = asyncio.get_running_loop()
        if self.task is None:
            self.task = asyncio.current_task()
        for sig in self.SIGNALS:
            try:
                self._loop.add_signal_handler(sig, self._handle, sig)
            except (NotImplementedError, RuntimeError):  # pragma: no cover — fora do Unix
                pass
        return self

    def on_meeting_stop(self, callback: Callable[[], None]) -> None:
        self._callback = callback
        if self.requested:
            callback()

    def _handle(self, sig: int) -> None:
        self.requested = True
        log.warning(
            "Sinal %s recebido — encerrando a reunião e gerando a ata"
            " (envie de novo para sair sem a ata)",
            signal.Signals(sig).name,
        )
        # Segundo sinal: sai na hora. Handler síncrono do Python, que roda mesmo
        # com o loop ocupado (ex.: retranscrição em lote).
        if self._loop is not None:
            for s in self.SIGNALS:
                try:
                    self._loop.remove_signal_handler(s)
                except (NotImplementedError, RuntimeError):  # pragma: no cover
                    pass
        for s in self.SIGNALS:
            signal.signal(s, _force_exit)
        if self._callback is not None:
            self._callback()
        elif self.task is not None:
            self.task.cancel()


def _force_exit(signum, _frame) -> None:  # pragma: no cover — encerra o processo
    log.error("Segundo sinal (%s) — saindo sem gerar a ata", signal.Signals(signum).name)
    logging.shutdown()
    os._exit(1)


def provisional_save(store: HistoryStore, record: MeetingRecord) -> None:
    """Registro provisório: o resummarize encontra a reunião mesmo se o processo morrer."""
    try:
        store.save(record, "")
    except Exception:  # noqa: BLE001 — histórico provisório é acessório
        log.exception("Falha ao salvar o registro provisório")
