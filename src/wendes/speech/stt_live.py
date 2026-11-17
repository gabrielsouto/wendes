"""STT ao vivo: VAD (Silero via faster-whisper/onnx) + whisper small int8.

Consome chunks PCM de uma fila inscrita na captura (AudioCapture.subscribe),
detecta fim de fala por silêncio e devolve utterances transcritas.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import numpy as np

from wendes.config import Config
from wendes.speech.stt_batch import resolve_stt_device, stt_cpu_threads

log = logging.getLogger(__name__)


@dataclass
class Utterance:
    text: str
    duration_s: float


class LiveStt:
    def __init__(self, cfg: Config) -> None:
        from faster_whisper import WhisperModel  # import pesado: só ao criar o STT

        self.model = WhisperModel(
            cfg.stt.live_model,
            device=resolve_stt_device(cfg),
            compute_type=cfg.stt.compute_type,
            cpu_threads=stt_cpu_threads(cfg),
            download_root=str(cfg.resolve(cfg.stt.download_root)),
        )
        self.vocabulary = cfg.stt.vocabulary
        self.silence_s = cfg.meeting.end_of_turn_silence_seconds

    def _transcribe(self, pcm: bytes) -> str:
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        segments, _ = self.model.transcribe(
            audio, language="pt", initial_prompt=self.vocabulary or None
        )
        return " ".join(s.text.strip() for s in segments).strip()

    @staticmethod
    def _has_speech(pcm: bytes) -> bool:
        from faster_whisper.vad import VadOptions, get_speech_timestamps

        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        return bool(get_speech_timestamps(audio, VadOptions(min_speech_duration_ms=200)))

    async def listen_utterance(
        self,
        chunks: asyncio.Queue[bytes],
        max_seconds: float,
        min_speech_chunks: int = 1,
        on_speech_end=None,
    ) -> Utterance | None:
        """Escuta até detectar fala seguida de `silence_s` de silêncio, ou estourar
        max_seconds. Retorna None se ninguém falou.

        on_speech_end: corrotina chamada assim que o fim da fala é detectado,
        ANTES da transcrição (que pode ser lenta) — usada para dar feedback
        imediato ao falante."""
        loop = asyncio.get_event_loop()
        buffer = bytearray()
        speech_chunks = 0
        silent_run = 0.0
        elapsed = 0.0
        chunk_s = 0.5

        # descarta áudio acumulado enquanto o bot falava
        while not chunks.empty():
            chunks.get_nowait()

        while elapsed < max_seconds:
            try:
                chunk = await asyncio.wait_for(chunks.get(), timeout=5)
            except asyncio.TimeoutError:
                elapsed += 5
                continue
            elapsed += chunk_s
            buffer.extend(chunk)
            has = await loop.run_in_executor(None, self._has_speech, chunk)
            if has:
                speech_chunks += 1
                silent_run = 0.0
            else:
                silent_run += chunk_s
                if speech_chunks >= min_speech_chunks and silent_run >= self.silence_s:
                    break

        if speech_chunks < min_speech_chunks:
            return None
        if on_speech_end is not None:
            await on_speech_end()
        text = await loop.run_in_executor(None, self._transcribe, bytes(buffer))
        if not text:
            return None
        log.info("Utterance (%.1fs): %s", elapsed, text[:120])
        return Utterance(text=text, duration_s=elapsed)
