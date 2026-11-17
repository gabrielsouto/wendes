from __future__ import annotations

from conftest import FakeLlm
from wendes.minutes.summarizer import _ask_ata, _split_chunks


def test_split_chunks_respeita_quebras_de_linha():
    text = "\n".join(["a" * 10] * 10)  # 10 linhas de 10 chars (+1 da quebra)
    chunks = _split_chunks(text, chunk_chars=35)
    assert all(len(c) <= 35 for c in chunks)
    assert "\n".join(chunks) == text
    assert len(chunks) == 4  # 3 + 3 + 3 + 1 linhas


def test_split_chunks_texto_curto_e_vazio():
    assert _split_chunks("uma linha", chunk_chars=100) == ["uma linha"]
    assert _split_chunks("", chunk_chars=100) == []


def test_split_chunks_linha_maior_que_o_bloco_fica_inteira():
    chunks = _split_chunks("x" * 50 + "\ny", chunk_chars=10)
    assert chunks == ["x" * 50, "y"]


def test_ask_ata_com_fim():
    llm = FakeLlm(["RESUMO:\nTudo andando.\nACOES:\n- revisar PR\nFIM"])
    assert _ask_ata(llm, "prompt") == ("Tudo andando.", "- revisar PR")


def test_ask_ata_sem_fim_ainda_aproveita(caplog):
    llm = FakeLlm(["RESUMO:\nTudo andando.\nACOES:\n- revisar PR"])
    assert _ask_ata(llm, "prompt") == ("Tudo andando.", "- revisar PR")
    assert "FIM" in caplog.text


def test_ask_ata_fora_do_formato_retorna_none():
    assert _ask_ata(FakeLlm(["Aqui está a ata, sem seções."]), "p") is None
    assert _ask_ata(FakeLlm([None]), "p") is None
    assert _ask_ata(FakeLlm(fail=True), "p") is None
