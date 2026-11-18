from __future__ import annotations

import pytest

from wendes.assistant.triggers import VoiceTrigger, parse_chat


@pytest.mark.parametrize(
    ("text", "private", "expected"),
    [
        ("/wendes quem ficou de revisar o PR?", False, ("ask", "quem ficou de revisar o PR?")),
        ("@wendes, qual a data?", False, ("ask", "qual a data?")),
        ("@Wendes qual a data?", False, ("ask", "qual a data?")),
        ("qual a data?", True, ("ask", "qual a data?")),
        ("qual a data?", False, None),
        ("/resumo", False, ("resumo", "")),
        ("/ações", False, ("acoes", "")),
        ("/encerrar", False, ("fim", "")),
        ("/auth 123456", True, ("auth", "123456")),
        ("/xyz", False, ("desconhecido", "/xyz")),
        ("@fulana oi", False, None),
    ],
)
def test_parse_chat(text, private, expected):
    cmd = parse_chat(text, "Wendes", private)
    if expected is None:
        assert cmd is None
    else:
        assert (cmd.name, cmd.arg) == expected


def test_parse_artefato_com_referencia():
    cmd = parse_chat("/doc #2 acrescenta o orçamento", "Wendes")
    assert (cmd.name, cmd.kind, cmd.ref) == ("artifact", "doc", 2)
    assert cmd.arg == "acrescenta o orçamento"
    cmd = parse_chat("/diagrama fluxo de aprovação", "Wendes")
    assert (cmd.kind, cmd.ref, cmd.arg) == ("diagrama", None, "fluxo de aprovação")


def _vt():
    return VoiceTrigger(["Wendes", "vendes", "wendis", "uendes"])


@pytest.mark.parametrize(
    "text",
    [
        "Wendes, quem ficou de revisar o PR?",
        "Ô Vendes, faz um resumo pra gente",
        "Olha, Wendis, qual foi a data combinada?",
        "e aí uendes desenha o fluxo de aprovação",
    ],
)
def test_ativacao_positiva(text):
    req = _vt().check(0, 3, text)
    assert req is not None
    assert "wendes" not in req.text.lower()
    assert len(req.text.split()) >= 3


@pytest.mark.parametrize(
    "text",
    [
        "você vende isso?",
        "Wendes, tudo bem?",  # restante com menos de 3 palavras
        "ontem eu falei com o Wendes sobre o deploy",  # ativação fora das 3 primeiras
        "a gente vende o produto para três clientes",
        "",
    ],
)
def test_ativacao_negativa(text):
    assert _vt().check(0, 3, text) is None


def test_palavra_isolada_e_pergunta_no_proximo_enunciado_do_mesmo_falante():
    vt = _vt()
    assert vt.check(10, 11, "Wendes?", speaker="Fulana") is None
    assert vt.check(12, 14, "acho que sim", speaker="Beltrano") is None
    req = vt.check(15, 18, "quem ficou com o deploy?", speaker="Fulana")
    assert req is not None and req.text == "quem ficou com o deploy?"


def test_palavra_isolada_falante_desconhecido_vale_ate_8s():
    vt = _vt()
    assert vt.check(10, 11, "Wendes.") is None
    assert vt.check(20, 22, "quem ficou com o deploy?") is None  # passou de 8 s
    vt = _vt()
    vt.check(10, 11, "Wendes.")
    assert vt.check(13, 15, "quem ficou com o deploy?").text == "quem ficou com o deploy?"


def test_intervalo_minimo_entre_ativacoes():
    vt = _vt()
    assert vt.check(0, 2, "Wendes, quem ficou de revisar?") is not None
    assert vt.check(5, 7, "Wendes, e a data do evento?") is None
    assert vt.check(11, 13, "Wendes, e a data do evento?") is not None
