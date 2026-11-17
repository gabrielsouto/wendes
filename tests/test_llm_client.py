from __future__ import annotations

from conftest import make_cfg
from wendes.llm.client import Llm


def test_usa_a_chave_do_ambiente(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-teste-123")
    assert Llm(make_cfg()).client.api_key == "sk-teste-123"


def test_sem_chave_usa_placeholder(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert Llm(make_cfg()).client.api_key == "none"
