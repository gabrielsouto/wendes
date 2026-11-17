from __future__ import annotations

import asyncio
import json
import time

import pytest
from pydantic import BaseModel

from conftest import FakeLlm
from wendes.assistant.budget import Part, estimate_tokens, fit, lines_tokens
from wendes.assistant.llm_queue import (
    BACKGROUND,
    HUMAN,
    NOTES,
    LlmScheduler,
    ask_json,
    extract_json,
    structured_kwargs,
)


class Resp(BaseModel):
    resposta: str


def test_extract_json_tolera_cercas_e_texto():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Claro! Aqui está: {"a": {"b": 2}} espero ter ajudado') == {"a": {"b": 2}}
    assert extract_json('lixo { quebrado {"ok": true}') == {"ok": True}
    with pytest.raises(ValueError):
        extract_json("sem json nenhum")


def test_ask_json_lixo_depois_valido_faz_nova_tentativa():
    llm = FakeLlm(responses=["não sei fazer json", '{"resposta": "ok"}'])
    out = ask_json(llm, "pergunta", Resp)
    assert out == Resp(resposta="ok")
    assert len(llm.prompts) == 2
    assert "não era um JSON válido" in llm.prompts[1]


def test_ask_json_lixo_duas_vezes_devolve_none():
    llm = FakeLlm(responses=["lixo", '{"outra": 1}'])
    assert ask_json(llm, "pergunta", Resp) is None
    assert len(llm.prompts) == 2


def test_structured_kwargs():
    assert structured_kwargs("json_object", Resp) == {"response_format": {"type": "json_object"}}
    assert "guided_json" in structured_kwargs("vllm_guided", Resp)["extra_body"]
    sk = structured_kwargs("vllm_guided", Resp, "structured_outputs")["extra_body"]
    assert sk["structured_outputs"]["json"]["properties"]["resposta"]
    assert structured_kwargs("ollama_format", Resp)["extra_body"]["format"]["type"] == "object"
    assert structured_kwargs("none", Resp) == {}


def test_fit_respeita_orcamento_e_corta_transcricao_antiga_primeiro():
    transcript = [f"[00:{i:02d}] Fulana: fala número {i} " + "x" * 50 for i in range(60)]
    notes = ["# Tópico", "- ponto importante " * 3]
    parts = [
        Part("notas", notes, 500, keep="first", cut_order=5),
        Part("transcricao", transcript, 2000, keep="newest", cut_order=0),
    ]
    out = fit(parts, 600)
    total = sum(lines_tokens(v) for v in out.values())
    assert total <= 600
    assert out["notas"] == notes  # as notas ficam
    assert out["transcricao"][-1] == transcript[-1]  # o mais recente fica
    assert transcript[0] not in out["transcricao"]
    assert estimate_tokens("a" * 35) == 10


async def test_scheduler_prioridade_e_fusao():
    sched = LlmScheduler(max_concurrency=1)
    gate = asyncio.Event()
    loop = asyncio.get_running_loop()

    def blocking():
        asyncio.run_coroutine_threadsafe(gate.wait(), loop).result(5)
        return "primeiro"

    sched.start()
    first = sched.submit(BACKGROUND, "compactar", blocking)
    await asyncio.sleep(0.05)  # o primeiro já está rodando
    n1 = sched.submit(NOTES, "notas", lambda: "n", key="notas")
    n2 = sched.submit(NOTES, "notas", lambda: "n2", key="notas")
    assert n1 is n2  # novo gatilho funde com o pendente
    bg = sched.submit(BACKGROUND, "reparo", lambda: "bg")
    human = sched.submit(HUMAN, "pergunta", lambda: "h")
    assert sched.size == 4
    gate.set()
    assert await asyncio.gather(first, n1, bg, human) == ["primeiro", "n", "bg", "h"]
    assert sched.order == ["compactar", "pergunta", "notas", "reparo"]
    await sched.join(1)
    assert sched.size == 0
    await sched.close()


async def test_scheduler_timeout_e_telemetria(tmp_path):
    sched = LlmScheduler(max_concurrency=1, telemetry_path=tmp_path / "llm_calls.jsonl")
    sched.start()
    with pytest.raises(asyncio.TimeoutError):
        await sched.submit(HUMAN, "pergunta", lambda: time.sleep(0.3), timeout=0.05)
    assert await sched.submit(NOTES, "notas", lambda: 1, est_tokens=42) == 1
    await sched.close()
    rows = [json.loads(x) for x in (tmp_path / "llm_calls.jsonl").read_text().splitlines()]
    assert [(r["tipo"], r["ok"]) for r in rows] == [("pergunta", False), ("notas", True)]
    assert rows[1]["tokens_est"] == 42
    assert "prompt" not in rows[0]


async def test_scheduler_close_cancela_pendentes():
    sched = LlmScheduler()
    fut = sched.submit(HUMAN, "x", lambda: 1)
    await sched.close()
    assert fut.cancelled()
