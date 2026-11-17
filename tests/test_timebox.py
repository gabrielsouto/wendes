from __future__ import annotations

import pytest

from wendes.facilitator.state_machine import question_budget


def test_divisao_inicial_igual():
    assert question_budget(120, 3, 0, 0, 30) == pytest.approx(40)


def test_resposta_rapida_libera_tempo_para_as_seguintes():
    # 1ª resposta em 10 s: sobram 110 s para 2 perguntas
    assert question_budget(120, 3, 1, 10, 30) == pytest.approx(55)
    # 2ª também rápida (mais 10 s): a última fica com 100 s
    assert question_budget(120, 3, 2, 20, 30) == pytest.approx(100)


def test_piso_respeitado():
    # 1ª resposta longa (100 s): sobram 20 s / 2 = 10 s, mas o piso é 30
    assert question_budget(120, 3, 1, 100, 30) == 30


def test_orcamento_esgotado_cai_no_piso():
    assert question_budget(120, 3, 2, 120, 30) == 30
    assert question_budget(120, 3, 2, 500, 30) == 30


def test_total_da_pessoa_fica_no_timebox_com_respostas_ate_o_limite():
    used = 0.0
    for idx in range(3):
        used += question_budget(120, 3, idx, used, 10)
    assert used == pytest.approx(120)


def test_sem_perguntas_restantes_nao_divide_por_zero():
    assert question_budget(120, 3, 3, 0, 30) == 120
