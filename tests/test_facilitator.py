from __future__ import annotations

import asyncio
import time

import pytest

import wendes.facilitator.state_machine as sm
from conftest import FakeLlm, FakeRoom, make_cfg
from wendes.facilitator.commands import ChatCommands
from wendes.speech.stt_live import Utterance


class FakeCapture:
    def subscribe(self, maxsize: int = 120) -> asyncio.Queue:
        return asyncio.Queue(maxsize=maxsize)


class FakeTts:
    def __init__(self, delay_s: float = 0.0) -> None:
        self.delay_s = delay_s
        self.spoken: list[str] = []

    def synthesize(self, text: str):
        self.spoken.append(text)
        if self.delay_s:
            time.sleep(self.delay_s)
        return "/tmp/fake.wav"


class FakeStt:
    def __init__(self, text: str) -> None:
        self.text = text

    async def listen_utterance(self, chunks, max_seconds, on_speech_end=None, **_):
        if on_speech_end is not None:
            await on_speech_end()
        return Utterance(text=self.text, duration_s=5.0)


async def _noop_play(path: str) -> None:
    return None


@pytest.fixture
def facilitator_factory(monkeypatch):
    monkeypatch.setattr(sm, "play_wav", _noop_play)

    def build(llm, tts=None, text="tenho um impedimento com o deploy"):
        cfg = make_cfg(commands={"start_on_command": False})
        room = FakeRoom({"p1": "Fulana da Silva", "p2": "Beltrano Souza"})
        commands = ChatCommands(cfg, room)
        fac = sm.Facilitator(
            cfg, room, FakeCapture(), tts or FakeTts(), FakeStt(text), llm, commands
        )
        return fac, commands

    return build


async def test_encerrar_durante_analise_do_llm_retorna_rapido(facilitator_factory):
    llm = FakeLlm(["IMPEDIMENTO: sim | FOLLOWUP: Quem pode ajudar?"], delay_s=2)
    fac, commands = facilitator_factory(llm)

    async def encerrar_logo():
        await asyncio.sleep(0.1)
        commands.request_end()

    asyncio.create_task(encerrar_logo())
    started = time.monotonic()
    record = await asyncio.wait_for(fac.run(), timeout=1)
    assert time.monotonic() - started < 1

    assert len(llm.prompts) == 1  # a análise começou e foi abandonada
    person = record.people[0]
    assert person.name == "Fulana da Silva"
    assert person.present
    assert [a.text for a in person.answers] == ["tenho um impedimento com o deploy"] * 3


async def test_pular_durante_analise_segue_para_a_proxima_pessoa(facilitator_factory):
    llm = FakeLlm(["IMPEDIMENTO: sim | FOLLOWUP: nenhum"], delay_s=0.5)
    fac, commands = facilitator_factory(llm)

    async def pular_uma_vez():
        await asyncio.sleep(0.1)
        commands.skip.set()

    asyncio.create_task(pular_uma_vez())
    record = await asyncio.wait_for(fac.run(), timeout=3)
    assert [p.name for p in record.people] == ["Fulana da Silva", "Beltrano Souza"]
    assert record.people[0].has_blocker is False  # análise pulada
    assert record.people[1].has_blocker is True


async def test_tts_lento_nao_trava_o_loop(facilitator_factory):
    fac, _ = facilitator_factory(FakeLlm(), tts=FakeTts(delay_s=0.3))
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    t = asyncio.create_task(ticker())
    await fac.say("Bom dia")
    t.cancel()
    assert ticks >= 10  # o loop seguiu girando enquanto o Piper "sintetizava"
