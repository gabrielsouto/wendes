"""Modo replay: roda o pipeline do assist a partir de um `recording.wav` gravado.

- `ReplayCapture` entrega o WAV em pedaços de 0,5 s com a mesma interface do
  `AudioCapture` (inclusive `samples_written`, `paused` e filas com tempo).
  `speed` controla o ritmo: 1 = tempo real, 4 = quatro vezes mais rápido,
  0 = o mais rápido possível (com contrapressão: espera a transcrição).
- `TerminalRoom` imita a sala: o "chat" é impresso na tela e os comandos são
  lidos do stdin (ex.: `/resumo`, `/wendes quem ficou de revisar o PR?`). O
  operador do terminal é admin.
- Se a reunião original tiver `audio_levels.jsonl`, os níveis são reproduzidos
  no `SpeakerTracker` conforme a posição do áudio.

É a ferramenta principal para ajustar prompts sem precisar de reunião.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import wave
from dataclasses import dataclass
from pathlib import Path

from wendes.audio.capture import CHUNK_BYTES, CHUNK_SECONDS, SAMPLE_RATE, AudioCapture

log = logging.getLogger(__name__)

OPERATOR_ID = "terminal"
OPERATOR_NAME = "Operador"


class ReplayCapture(AudioCapture):
    def __init__(self, source_wav: Path, record_path: Path, speed: float = 1.0) -> None:
        super().__init__(record_path, source=str(source_wav))
        self.source_wav = source_wav
        self.speed = speed
        self.done = asyncio.Event()
        self.listeners: list = []  # callbacks(posição) chamados a cada pedaço

    async def start(self) -> None:
        self.record_path.parent.mkdir(parents=True, exist_ok=True)
        self._wav = wave.open(str(self.record_path), "wb")
        self._wav.setnchannels(1)
        self._wav.setsampwidth(2)
        self._wav.setframerate(SAMPLE_RATE)
        self._task = asyncio.create_task(self._reader())
        log.info("Replay de %s (velocidade %s)", self.source_wav, self.speed or "máxima")

    async def _reader(self) -> None:
        try:
            with wave.open(str(self.source_wav), "rb") as src:
                if (src.getnchannels(), src.getsampwidth(), src.getframerate()) != (
                    1, 2, SAMPLE_RATE,
                ):
                    raise ValueError("replay espera WAV mono 16 kHz s16le (o recording.wav)")
                frames = CHUNK_BYTES // 2
                while True:
                    data = src.readframes(frames)
                    if not data:
                        break
                    if self.speed == 0:
                        # contrapressão: não atropela a transcrição
                        while any(q.full() for q in self._subscribers):
                            await asyncio.sleep(0.01)
                    self._ingest(data)
                    for cb in self.listeners:
                        cb(self.position_s)
                    await asyncio.sleep(CHUNK_SECONDS / self.speed if self.speed else 0)
        except asyncio.CancelledError:
            pass
        finally:
            self.done.set()

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self._wav is not None:
            self._wav.close()
            self._wav = None


class LevelReplayer:
    """Reproduz `audio_levels.jsonl` gravado no tracker, conforme a posição do áudio."""

    def __init__(self, path: Path, tracker) -> None:
        from wendes.assistant.speakers import load_levels

        self.levels, self.dominant, names = load_levels(path)
        self.tracker = tracker
        self._i = 0
        for pid, name in names.items():
            tracker.set_name(pid, name)

    def __call__(self, position: float) -> None:
        from wendes.assistant.speakers import LevelEvent

        while self._i < len(self.levels) and self.levels[self._i].t <= position:
            ev = self.levels[self._i]
            self.tracker._events.append(LevelEvent(ev.t, ev.pid, ev.level))
            self._i += 1


@dataclass
class TerminalParticipant:
    id: str
    display_name: str


class TerminalRoom:
    """Sala falsa de terminal com a interface de `JitsiRoom` usada pelo assist."""

    can_send_private = True
    log_text = False

    def __init__(self, out=None) -> None:
        self.out = out or sys.stdout
        self.participants = {OPERATOR_ID: OPERATOR_NAME}
        self._chat: list = []
        self._participant: list = []

    def _print(self, text: str) -> None:
        print(text, file=self.out, flush=True)

    async def send_chat(self, message: str) -> None:
        self._print(f"💬 [chat] {message}")

    async def send_private(self, participant_id: str, message: str) -> None:
        who = self.participants.get(participant_id, participant_id)
        self._print(f"💬 [privado → {who}] {message}")

    async def list_participants(self) -> list[TerminalParticipant]:
        return [TerminalParticipant(pid, n) for pid, n in self.participants.items()]

    async def get_display_name(self, participant_id: str) -> str:
        return self.participants.get(participant_id, "")

    def on_chat_message(self, cb) -> None:
        self._chat.append(cb)

    def on_participant(self, cb) -> None:
        self._participant.append(cb)

    def on_dominant_speaker(self, cb) -> None:
        pass

    def on_audio_levels(self, cb) -> None:
        pass

    async def emit_chat(self, pid: str, text: str, private: bool = False) -> None:
        for cb in self._chat:
            res = cb(pid, text, private)
            if asyncio.iscoroutine(res):
                await res

    async def mute(self) -> None:
        pass

    async def unmute(self) -> None:
        pass

    async def hangup(self) -> None:
        pass

    async def read_stdin(self, on_eof=None) -> None:
        """Lê comandos do stdin; `!texto` manda como mensagem privada ao bot."""
        loop = asyncio.get_running_loop()
        self._print("Replay: digite comandos do chat (/resumo, /wendes …, /fim). "
                    "Comece com ! para mensagem privada.")
        while True:
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:
                if on_eof is not None:
                    on_eof()
                return
            line = line.strip()
            if not line:
                continue
            private = line.startswith("!")
            await self.emit_chat(OPERATOR_ID, line[1:].strip() if private else line, private)


async def terminal_speak(text: str) -> None:
    print(f"🔊 [voz] {text}", flush=True)
