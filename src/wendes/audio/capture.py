"""Captura o áudio da sala de bot_out.monitor (16 kHz mono s16le via parec).

Grava o WAV completo usado na retranscrição pós-reunião e distribui cada
pedaço às filas inscritas (ex.: STT ao vivo). Sem inscritos nada se acumula
— o modo stealth não se inscreve. Cada fila é limitada: se o consumidor
atrasar, o pedaço mais antigo é descartado.

Com `paused` (modo assist, /pausar), o WAV recebe silêncio digital do mesmo
tamanho — a linha do tempo não desalinha — e nada é distribuído.
"""
from __future__ import annotations

import asyncio
import logging
import time
import wave
from pathlib import Path

log = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
CHUNK_SECONDS = 0.5
CHUNK_BYTES = int(SAMPLE_RATE * 2 * CHUNK_SECONDS)  # s16le mono
DROP_WARN_INTERVAL_S = 60.0


class AudioCapture:
    def __init__(self, record_path: Path, source: str = "bot_out.monitor") -> None:
        self.source = source
        self.record_path = record_path
        self._subscribers: list[asyncio.Queue] = []
        self._timed: set[int] = set()  # ids das filas que recebem (amostra inicial, pedaço)
        self.paused = False
        self.dropped_chunks = 0
        self._last_drop_warn = float("-inf")
        self.samples_written = 0  # amostras gravadas no WAV (relógio do áudio)
        self._wav: wave.Wave_write | None = None
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

    @property
    def position_s(self) -> float:
        """Posição atual da gravação em segundos (relógio do áudio, não de parede)."""
        return self.samples_written / SAMPLE_RATE

    def subscribe(self, maxsize: int = 120, timed: bool = False) -> asyncio.Queue:
        """Nova fila que recebe cada pedaço de áudio (120 × 0,5 s = 1 min por padrão).

        Com `timed`, cada item é `(amostra inicial, pedaço)`: o consumidor sabe a
        posição exata do pedaço na gravação mesmo se algum for descartado.
        """
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._subscribers.append(q)
        if timed:
            self._timed.add(id(q))
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._subscribers:
            self._subscribers.remove(q)
            self._timed.discard(id(q))

    def _item(self, q: asyncio.Queue, data: bytes, start_sample: int):
        return (start_sample, data) if id(q) in self._timed else data

    def _dispatch(self, data: bytes, start_sample: int | None = None) -> None:
        """Entrega o pedaço a cada inscrito; fila cheia descarta o mais antigo."""
        if start_sample is None:
            start_sample = self.samples_written - len(data) // 2
        for q in self._subscribers:
            item = self._item(q, data, start_sample)
            try:
                q.put_nowait(item)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:  # pragma: no cover — corrida impossível no loop
                    pass
                q.put_nowait(item)
                self.dropped_chunks += 1
                now = time.monotonic()
                if now - self._last_drop_warn >= DROP_WARN_INTERVAL_S:
                    self._last_drop_warn = now
                    log.warning(
                        "Consumidor de áudio atrasado — %d pedaços descartados até agora",
                        self.dropped_chunks,
                    )

    def _write(self, data: bytes) -> None:
        if self._wav is not None:
            self._wav.writeframes(data)
        self.samples_written += len(data) // 2  # s16le: 2 bytes por amostra

    def _ingest(self, data: bytes) -> None:
        """Grava e distribui um pedaço; pausado, grava silêncio e não distribui."""
        start = self.samples_written
        if self.paused:
            self._write(b"\x00" * len(data))
            return
        self._write(data)
        self._dispatch(data, start)

    async def _reader(self) -> None:
        assert self._proc and self._proc.stdout
        try:
            while True:
                data = await self._proc.stdout.readexactly(CHUNK_BYTES)
                self._ingest(data)
        except (asyncio.IncompleteReadError, asyncio.CancelledError):
            pass

    async def stop(self) -> None:
        """Para a captura; tolera o parec já morto e sempre fecha o WAV."""
        if self._task:
            self._task.cancel()
        try:
            if self._proc and self._proc.returncode is None:
                try:
                    self._proc.terminate()
                except ProcessLookupError:
                    pass
                await self._proc.wait()
        finally:
            if self._wav is not None:
                self._wav.close()  # fecha o cabeçalho: sem isso o WAV fica ilegível
                self._wav = None
        log.info("Captura encerrada")
