"""Fakes reutilizáveis: os testes não dependem de Jitsi, navegador, áudio,
modelos baixados nem de LLM no ar."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

import pytest

from wendes.config import Config


@dataclass
class FakeParticipant:
    id: str
    display_name: str


class FakeRoom:
    """Imita a interface de `JitsiRoom` usada pelo facilitador e pelos comandos."""

    def __init__(self, participants: dict[str, str] | None = None) -> None:
        # participant_id -> display name
        self.participants: dict[str, str] = dict(participants or {})
        self.sent: list[str] = []
        self.private_sent: list[tuple[str, str]] = []
        self._chat_listeners: list = []
        self._speaker_listeners: list = []
        self._level_listeners: list = []
        self._participant_listeners: list = []
        self.can_send_private: bool | None = True
        self.log_text = True
        self.muted = True

    async def send_chat(self, message: str) -> None:
        self.sent.append(message)

    async def send_private(self, participant_id: str, message: str) -> None:
        self.private_sent.append((participant_id, message))

    async def list_participants(self) -> list[FakeParticipant]:
        return [FakeParticipant(pid, name) for pid, name in self.participants.items()]

    async def get_display_name(self, participant_id: str) -> str:
        return self.participants.get(participant_id, "")

    def on_chat_message(self, callback) -> None:
        self._chat_listeners.append(callback)

    def on_dominant_speaker(self, callback) -> None:
        self._speaker_listeners.append(callback)

    async def emit_chat(self, pid: str, text: str, private: bool = False) -> None:
        for cb in self._chat_listeners:
            res = cb(pid, text, private)
            if asyncio.iscoroutine(res):
                await res

    def emit_dominant_speaker(self, pid: str, display_name: str = "") -> None:
        for cb in self._speaker_listeners:
            cb(pid, display_name)

    def on_audio_levels(self, callback) -> None:
        self._level_listeners.append(callback)

    def on_participant(self, callback) -> None:
        self._participant_listeners.append(callback)

    def emit_levels(self, items: list[tuple[str, float]]) -> None:
        for cb in self._level_listeners:
            cb(items)

    async def emit_participant(self, kind: str, pid: str, name: str = "") -> None:
        if kind == "leave":
            self.participants.pop(pid, None)
        elif name:
            self.participants[pid] = name
        for cb in self._participant_listeners:
            res = cb(kind, pid, name)
            if asyncio.iscoroutine(res):
                await res

    async def unmute(self) -> None:
        self.muted = False

    async def mute(self) -> None:
        self.muted = True

    async def hangup(self) -> None:
        pass


@dataclass
class FakeLlm:
    """LLM roteirizado: devolve as respostas em ordem (a última se repete)."""

    responses: list[str | None] = field(default_factory=list)
    delay_s: float = 0.0
    fail: bool = False
    prompts: list[str] = field(default_factory=list)

    def ask(self, prompt: str, **kwargs: Any) -> str:
        self.prompts.append(prompt)
        if self.delay_s:
            time.sleep(self.delay_s)  # simula a chamada bloqueante do cliente OpenAI
        if self.fail:
            raise RuntimeError("LLM fora do ar")
        if not self.responses:
            return ""
        resp = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        return resp or ""

    def ask_or_none(self, prompt: str, **kwargs: Any) -> str | None:
        try:
            return self.ask(prompt, **kwargs) or None
        except Exception:  # noqa: BLE001 — mesmo contrato do Llm real
            return None


class FakeTranscribe:
    """Transcrição falsa: mapeia o índice do enunciado (0, 1, …) para um texto."""

    def __init__(self, texts: dict[int, str] | list[str] | None = None, delay_s: float = 0.0):
        if isinstance(texts, list):
            texts = dict(enumerate(texts))
        self.texts = texts or {}
        self.delay_s = delay_s
        self.calls: list[int] = []  # tamanho em bytes de cada enunciado recebido

    def __call__(self, pcm: bytes) -> str:
        idx = len(self.calls)
        self.calls.append(len(pcm))
        if self.delay_s:
            time.sleep(self.delay_s)
        return self.texts.get(idx, f"enunciado {idx}")


def energy_vad(chunk: bytes) -> bool:
    """VAD falso por energia (o Silero não reconhece senoide como fala)."""
    import numpy as np

    audio = np.frombuffer(chunk, dtype=np.int16).astype(np.float32)
    return bool(audio.size) and float(np.sqrt(np.mean(audio**2))) > 500


def synth_pcm(pattern: list[tuple[str, float]], rate: int = 16_000) -> bytes:
    """Áudio sintético: [("tom", 1.5), ("silencio", 1.0), …] → PCM s16le mono."""
    import numpy as np

    parts = []
    for kind, seconds in pattern:
        n = int(rate * seconds)
        if kind == "tom":
            t = np.arange(n) / rate
            parts.append((np.sin(2 * np.pi * 440 * t) * 8000).astype(np.int16))
        else:
            parts.append(np.zeros(n, dtype=np.int16))
    return np.concatenate(parts).tobytes() if parts else b""


def write_wav(path, pcm: bytes, rate: int = 16_000):
    import wave

    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return path


def _deep_merge(base: dict, extra: dict) -> dict:
    out = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def make_cfg(**overrides: Any) -> Config:
    """Config válido sem ler arquivos; seções sobrepostas por merge raso por chave."""
    base = {
        "room": {"url": "https://meet.jit.si/SalaDeTeste", "bot_display_name": "Wendes"},
        "team": {
            "name": "Equipe Teste",
            "members": [
                {"name": "Fulana da Silva", "aliases": ["Fulana", "fsilva"]},
                {"name": "Beltrano Souza", "aliases": ["Beltrano"]},
            ],
        },
        "llm": {"base_url": "http://localhost:9/v1", "model": "fake"},
        "commands": {"admins": ["Fulana da Silva"]},
    }
    return Config.model_validate(_deep_merge(base, overrides))


@pytest.fixture
def cfg(tmp_path) -> Config:
    return make_cfg(data_dir=str(tmp_path / "data"))


@pytest.fixture
def fake_room() -> FakeRoom:
    return FakeRoom({"p1": "Fulana da Silva", "p2": "Beltrano Souza"})
