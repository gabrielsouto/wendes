"""Captura o áudio da sala de bot_out.monitor (16 kHz mono s16le via parec).

Alimenta simultaneamente o STT ao vivo (fila de chunks) e a gravação WAV
completa usada na retranscrição pós-reunião.
"""
from __future__ import annotations

import asyncio
import logging
import wave
from pathlib import Path

log = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
CHUNK_SECONDS = 0.5
CHUNK_BYTES = int(SAMPLE_RATE * 2 * CHUNK_SECONDS)  # s16le mono


class AudioCapture:
    def __init__(self, record_path: Path, source: str = "bot_out.monitor") -> None:
        self.source = source
        self.record_path = record_path
        self.chunks: asyncio.Queue[bytes] = asyncio.Queue()
        self._proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self.record_path.parent.mkdir(parents=True, exist_ok=True)
        self._wav = wave.open(str(self.record_path), "wb")
        self._wav.setnchannels(1)
        self._wav.setsampwidth(2)
        self._wav.setframerate(SAMPLE_RATE)
        self._proc = await asyncio.create_subprocess_exec(
            "parec",
            f"--device={self.source}",
            "--format=s16le",
            f"--rate={SAMPLE_RATE}",
            "--channels=1",
            stdout=asyncio.subprocess.PIPE,
        )
        self._task = asyncio.create_task(self._reader())
        log.info("Captura iniciada em %s → %s", self.source, self.record_path)

    async def _reader(self) -> None:
        assert self._proc and self._proc.stdout
        try:
            while True:
                data = await self._proc.stdout.readexactly(CHUNK_BYTES)
                self._wav.writeframes(data)
                await self.chunks.put(data)
        except (asyncio.IncompleteReadError, asyncio.CancelledError):
            pass

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        if self._proc:
            self._proc.terminate()
            await self._proc.wait()
        self._wav.close()
        log.info("Captura encerrada")
