from __future__ import annotations

from conftest import make_cfg
from wendes.speech.stt_batch import normalize_transcript_text, stt_hotwords


def test_normalize_aplica_correcoes_palavra_inteira_sem_caixa():
    corr = {"de ploy": "deploy", "jira": "Jira"}
    text = "Fiz o De Ploy e atualizei o JIRA; jirafa fica igual."
    assert normalize_transcript_text(text, corr) == (
        "Fiz o deploy e atualizei o Jira; jirafa fica igual."
    )


def test_normalize_sem_correcoes():
    assert normalize_transcript_text("texto", None) == "texto"
    assert normalize_transcript_text("texto", {}) == "texto"


def test_hotwords_explicitos_tem_prioridade():
    cfg = make_cfg(stt={"hotwords": ["Kubernetes", "Grafana"], "vocabulary": "daily"})
    assert stt_hotwords(cfg) == "Kubernetes, Grafana"


def test_hotwords_fallback_vocabulario_e_equipe():
    cfg = make_cfg(stt={"vocabulary": "daily, sprint"})
    assert stt_hotwords(cfg) == (
        "daily, sprint, Fulana da Silva, Fulana, fsilva, Beltrano Souza, Beltrano"
    )


def test_hotwords_vazio_vira_none():
    cfg = make_cfg(team={"members": []}, stt={"vocabulary": ""})
    assert stt_hotwords(cfg) is None
