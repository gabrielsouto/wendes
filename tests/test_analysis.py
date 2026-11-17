from __future__ import annotations

import pytest

from conftest import FakeLlm
from wendes.facilitator.analysis import analyze_blocker_answer


@pytest.mark.parametrize(
    "answer",
    ["Sem impedimento hoje", "Nenhum bloqueio", "não", "Não.", "Tudo certo por aqui"],
)
def test_marcadores_de_sem_impedimento_nao_chamam_llm(answer):
    llm = FakeLlm(["IMPEDIMENTO: sim | FOLLOWUP: nenhum"])
    result = analyze_blocker_answer(llm, "Fulana", answer)
    assert result.has_blocker is False
    assert result.followup is None
    assert llm.prompts == []


def test_llm_responde_sim_com_followup():
    llm = FakeLlm(["IMPEDIMENTO: sim | FOLLOWUP: Quem pode liberar o acesso?"])
    result = analyze_blocker_answer(llm, "Fulana", "Estou sem acesso ao banco")
    assert result.has_blocker is True
    assert result.followup == "Quem pode liberar o acesso?"
    assert "Estou sem acesso ao banco" in llm.prompts[0]


def test_llm_responde_nao():
    llm = FakeLlm(["IMPEDIMENTO: não | FOLLOWUP: nenhum"])
    result = analyze_blocker_answer(llm, "Fulana", "Talvez o deploy atrase um pouco")
    assert result.has_blocker is False
    assert result.followup is None


@pytest.mark.parametrize(
    "resp",
    [
        "IMPEDIMENTO: sim | FOLLOWUP: nenhum",
        "IMPEDIMENTO: sim | FOLLOWUP: <pergunta curta de esclarecimento>",
        "IMPEDIMENTO: sim | FOLLOWUP: " + "x" * 250,
        "IMPEDIMENTO: sim | FOLLOWUP: ",
    ],
)
def test_followup_invalido_e_descartado(resp):
    result = analyze_blocker_answer(FakeLlm([resp]), "Fulana", "Travado no deploy")
    assert result.has_blocker is True
    assert result.followup is None


def test_sem_llm_fallback_conservador():
    result = analyze_blocker_answer(None, "Fulana", "Travado no deploy")
    assert result.has_blocker is True
    assert result.followup is None


def test_llm_indisponivel_fallback_conservador():
    result = analyze_blocker_answer(FakeLlm(fail=True), "Fulana", "Travado no deploy")
    assert result.has_blocker is True
    assert result.followup is None
