"""Transcrição contínua do modo assist.

Inscreve-se no `AudioCapture` (fila com tempo), roda o VAD Silero em cada
pedaço de 0,5 s e fecha um enunciado com `silence_s` de silêncio ou ao atingir
`max_utterance_s`. Os enunciados vão para uma fila interna, transcrita por um
**único worker dedicado** (ThreadPoolExecutor de 1 thread), para que o Whisper
ao vivo nunca dispute CPU consigo mesmo nem trave o loop asyncio.

Todo tempo é em segundos desde o início da gravação, medido pelas amostras
gravadas. Se o áudio esperando transcrição passar de `max_lag_s`, os
enunciados mais antigos são descartados e vira um marcador de lacuna — a
gravação continua e a ata final usa a retranscrição em lote.
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import numpy as np

from wendes.config import Config

log = logging.getLogger(__name__)

SAMPLE_RATE = 16_000

TranscribeFn = Callable[[bytes], str]
VadFn = Callable[[bytes], bool]


@dataclass
class PendingUtterance:
    start: float
    end: float
    pcm: bytes


class ContinuousTranscriber:
    def __init__(
        self,
        queue: asyncio.Queue,
        *,
        transcribe: TranscribeFn,
        vad: VadFn,
        position_fn: Callable[[], float],
        on_segment: Callable[[float, float, str], Awaitable[None]],
        on_gap: Callable[[float, float], Awaitable[None] | None] | None = None,
        silence_s: float = 0.8,
        max_utterance_s: float = 20.0,
        max_lag_s: float = 30.0,
        vad_in_thread: bool = True,
    ) -> None:
        self.queue = queue
        self.transcribe = transcribe
        self.vad = vad
        self.position_fn = position_fn
        self.on_segment = on_segment
        self.on_gap = on_gap
        self.silence_s = silence_s
        self.max_utterance_s = max_utterance_s
        self.max_lag_s = max_lag_s
        self.vad_in_thread = vad_in_thread
        self.pending: deque[PendingUtterance] = deque()
        self.dropped = 0
        self.transcribed = 0
        self._buf: bytearray | None = None
        self._buf_start = 0.0
        self._buf_end = 0.0
        self._last_speech_end = 0.0
        self._silent = 0.0
        self._preroll: tuple[float, bytes] | None = None
        self._current_start: float | None = None
        self._wake = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()
        self._gaps: list[tuple[float, float]] = []
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stt-live")

    # ---- segmentação (síncrona e testável) ------------------------------
    def feed(self, start_sample: int, chunk: bytes, speech: bool) -> None:
        start = start_sample / SAMPLE_RATE
        end = start + len(chunk) / 2 / SAMPLE_RATE
        if self._buf is None:
            if not speech:
                self._preroll = (start, chunk)
                return
            self._buf = bytearray()
            self._buf_start = start
            pre = self._preroll
            if pre is not None and abs(pre[0] + len(pre[1]) / 2 / SAMPLE_RATE - start) < 1e-6:
                self._buf.extend(pre[1])  # um pedaço antes: não corta o início da fala
                self._buf_start = pre[0]
            self._preroll = None
            self._silent = 0.0
        self._buf.extend(chunk)
        self._buf_end = end
        if speech:
            self._silent = 0.0
            self._last_speech_end = end
        else:
            self._silent += end - start
        if self._silent >= self.silence_s or end - self._buf_start >= self.max_utterance_s:
            self._close()

    def flush(self) -> None:
        """Fecha o enunciado em curso (ex.: na pausa ou no fim do áudio)."""
        if self._buf is not None:
            self._close()

    def _close(self) -> None:
        assert self._buf is not None
        utt = PendingUtterance(self._buf_start, max(self._last_speech_end, self._buf_start),
                               bytes(self._buf))
        self._buf = None
        self._silent = 0.0
        self.pending.append(utt)
        self._idle.clear()
        self._check_lag()
        self._wake.set()

    # ---- atraso ----------------------------------------------------------
    @property
    def lag(self) -> float:
        oldest = self._current_start
        if self.pending:
            first = self.pending[0].start
            oldest = first if oldest is None else min(oldest, first)
        if oldest is None:
            return 0.0
        return max(0.0, self.position_fn() - oldest)

    def _check_lag(self) -> None:
        now = self.position_fn()
        dropped: list[PendingUtterance] = []
        while self.pending and now - self.pending[0].start > self.max_lag_s:
            dropped.append(self.pending.popleft())
        if dropped:
            self.dropped += len(dropped)
            gap = (dropped[0].start, dropped[-1].end)
            log.warning(
                "STT ao vivo atrasado (%.0f s > %.0f s) — %d enunciado(s) descartado(s)"
                " de %.1f a %.1f s; a ata usa a retranscrição em lote",
                now - dropped[0].start, self.max_lag_s, len(dropped), *gap,
            )
            self._gaps.append(gap)

    async def _emit_gaps(self) -> None:
        gaps, self._gaps = self._gaps, []
        if self.on_gap is None:
            return
        for start, end in gaps:
            res = self.on_gap(start, end)
            if asyncio.iscoroutine(res):
                await res

    # ---- tarefas ---------------------------------------------------------
    async def _segmenter(self) -> None:
        while True:
            start_sample, chunk = await self.queue.get()
            if self.vad_in_thread:
                speech = await asyncio.to_thread(self.vad, chunk)
            else:
                speech = self.vad(chunk)
            self.feed(start_sample, chunk, speech)
            if self._gaps:
                await self._emit_gaps()

    async def _worker(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            await self._wake.wait()
            self._wake.clear()
            while self.pending:
                self._check_lag()
                await self._emit_gaps()
                if not self.pending:
                    break
                utt = self.pending.popleft()
                self._current_start = utt.start
                try:
                    text = await loop.run_in_executor(self._executor, self.transcribe, utt.pcm)
                except Exception:  # noqa: BLE001 — um enunciado ruim não para a sessão
                    log.exception("Falha ao transcrever um enunciado ao vivo")
                    text = ""
                finally:
                    self._current_start = None
                text = (text or "").strip()
                if text:
                    self.transcribed += 1
                    await self.on_segment(utt.start, utt.end, text)
            if not self.pending:
                self._idle.set()

    async def run(self) -> None:
        try:
            await asyncio.gather(self._segmenter(), self._worker())
        finally:
            self._executor.shutdown(wait=False, cancel_futures=True)

    async def drain(self, timeout: float | None = None) -> None:
        """Fecha o enunciado em curso e espera a fila de transcrição esvaziar."""
        while not self.queue.empty():
            await asyncio.sleep(0.05)
        self.flush()
        await asyncio.wait_for(self._idle.wait(), timeout)


# ---- implementações reais (faster-whisper), fora dos testes ---------------
def silero_vad(chunk: bytes) -> bool:
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    audio = np.frombuffer(chunk, dtype=np.int16).astype(np.float32) / 32768.0
    return bool(get_speech_timestamps(audio, VadOptions(min_speech_duration_ms=200)))


def assist_hotwords(cfg: Config) -> str | None:
    from wendes.speech.stt_batch import stt_hotwords

    terms = [t for t in [stt_hotwords(cfg)] if t]
    a = cfg.assistant
    terms.append(cfg.room.bot_display_name)
    if a.wake_word:
        terms.append(a.wake_word)
    terms.extend(a.wake_word_aliases)
    return ", ".join(dict.fromkeys(t for t in terms if t)) or None


def make_whisper_transcriber(cfg: Config) -> TranscribeFn:
    """Whisper `stt.live_model` com os hotwords + nome do bot e aliases da ativação."""
    from faster_whisper import WhisperModel

    from wendes.speech.stt_batch import (
        normalize_transcript_text,
        resolve_stt_device,
        stt_cpu_threads,
    )

    model = WhisperModel(
        cfg.stt.live_model,
        device=resolve_stt_device(cfg),
        compute_type=cfg.stt.compute_type,
        cpu_threads=stt_cpu_threads(cfg),
        download_root=str(cfg.resolve(cfg.stt.download_root)),
    )
    hotwords = assist_hotwords(cfg)

    def transcribe(pcm: bytes) -> str:
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        segments, _ = model.transcribe(
            audio,
            language="pt",
            initial_prompt=cfg.stt.vocabulary or None,
            hotwords=hotwords,
            condition_on_previous_text=False,
            vad_filter=False,
        )
        text = " ".join(s.text.strip() for s in segments).strip()
        return normalize_transcript_text(text, cfg.stt.corrections)

    return transcribe
