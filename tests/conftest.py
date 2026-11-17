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
        self._chat_listeners: list = []
        self._speaker_listeners: list = []

    async def send_chat(self, message: str) -> None:
        self.sent.append(message)

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
            res = cb(pid, text)
            if asyncio.iscoroutine(res):
                await res

    def emit_dominant_speaker(self, pid: str, display_name: str = "") -> None:
        for cb in self._speaker_listeners:
            cb(pid, display_name)

    async def unmute(self) -> None:
        pass

    async def mute(self) -> None:
        pass

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
