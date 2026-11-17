from __future__ import annotations

import logging
import stat

import pytest

from conftest import FakeRoom, make_cfg
from wendes.facilitator.auth import LEAKED_MSG, MAX_FAILURES, AdminAuth, normalize_name
from wendes.facilitator.commands import ChatCommands
from wendes.facilitator.stealth import StealthSession

TEAM = {
    "members": [
        {"name": "Fulana da Silva", "aliases": ["fsilva"]},
        {"name": "Beltrano Souza", "aliases": ["Beltrano"]},
    ]
}


def _cfg(require_code: bool, **extra):
    return make_cfg(
        team=TEAM,
        commands={"admins": ["Fulana da Silva"], "require_code": require_code},
        **extra,
    )


def test_normalize_name():
    assert normalize_name("  Fulána   da SILVA ") == "fulana da silva"


@pytest.mark.parametrize(
    ("display", "expected"),
    [
        ("Fulana Hacker", False),
        ("Fulana", False),
        ("Fulana da Silva Hacker", False),
        ("fulana da silva", True),
        ("Fulána da Silva", True),
        ("  Fulana   da  Silva ", True),
        ("FSILVA", True),  # alias do membro admin
        ("Beltrano", False),  # membro, mas não admin
        ("", False),
    ],
)
def test_casamento_exato_por_nome_sem_codigo(display, expected):
    auth = AdminAuth(_cfg(require_code=False), FakeRoom())
    assert auth.code is None
    assert auth.is_admin("p9", display) is expected


def test_com_codigo_o_nome_e_irrelevante():
    auth = AdminAuth(_cfg(require_code=True), FakeRoom())
    assert len(auth.code) == 6 and auth.code.isdigit()
    assert auth.is_admin("p1", "Fulana da Silva") is False


async def test_auth_privado_com_codigo_certo_concede():
    room = FakeRoom({"p1": "Qualquer Nome"})
    auth = AdminAuth(_cfg(require_code=True), room)
    await auth.handle_auth("p1", auth.code, private=True)
    assert auth.is_admin("p1", "Qualquer Nome")
    assert room.private_sent and room.private_sent[0][0] == "p1"
    assert room.sent == []  # nada no chat público


async def test_auth_publico_nao_concede_e_troca_o_codigo(tmp_path):
    room = FakeRoom({"p1": "Fulana da Silva"})
    auth = AdminAuth(_cfg(require_code=True), room, run_dir=tmp_path)
    old = auth.code
    await auth.handle_auth("p1", old, private=False)
    assert not auth.is_admin("p1", "Fulana da Silva")
    assert auth.code != old
    assert room.sent == [LEAKED_MSG]
    assert (tmp_path / "admin_code.txt").read_text().strip() == auth.code
    # o código antigo não vale mais
    await auth.handle_auth("p1", old, private=True)
    assert not auth.is_admin("p1", "")


async def test_publish_code_grava_arquivo_0600(tmp_path, caplog):
    auth = AdminAuth(_cfg(require_code=True), FakeRoom(), run_dir=tmp_path)
    with caplog.at_level(logging.INFO):
        await auth.publish_code()
    path = tmp_path / "admin_code.txt"
    assert path.read_text().strip() == auth.code
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert auth.code in caplog.text


async def test_publish_code_envia_email_quando_configurado(monkeypatch):
    import wendes.distribution.email as email_mod

    sent: list[str] = []
    monkeypatch.setattr(email_mod, "send_admin_code", lambda cfg, code: sent.append(code))
    cfg = _cfg(
        require_code=True,
        distribution={"smtp_host": "smtp.local", "email_from": "bot@x"},
    )
    cfg.commands.admin_emails = ["fulana@x"]
    auth = AdminAuth(cfg, FakeRoom())
    await auth.publish_code()
    assert sent == [auth.code]


async def test_limite_de_tentativas(caplog):
    room = FakeRoom({"p1": "X"})
    auth = AdminAuth(_cfg(require_code=True), room)
    wrong = "000000" if auth.code != "000000" else "111111"
    with caplog.at_level(logging.WARNING):
        for _ in range(MAX_FAILURES):
            await auth.handle_auth("p1", wrong, private=True)
    assert "serão ignoradas" in caplog.text
    replies = len(room.private_sent)
    await auth.handle_auth("p1", auth.code, private=True)  # mesmo o certo é ignorado
    assert not auth.is_admin("p1", "X")
    assert len(room.private_sent) == replies
    # outro participante não é afetado
    await auth.handle_auth("p2", auth.code, private=True)
    assert auth.is_admin("p2", "")


async def test_chat_commands_recusa_encerrar_sem_auth():
    room = FakeRoom({"p1": "Fulana da Silva"})
    commands = ChatCommands(_cfg(require_code=True), room)
    await room.emit_chat("p1", "/encerrar")
    assert not commands.end.is_set()
    assert "não tem permissão" in room.sent[-1]
    assert "/auth" in room.sent[-1]

    await room.emit_chat("p1", f"/auth {commands.auth.code}", private=True)
    await room.emit_chat("p1", "/encerrar")
    assert commands.end.is_set()


async def test_chat_commands_permitir_concede_pelo_id():
    room = FakeRoom({"p1": "Fulana da Silva", "p2": "Beltrano Souza"})
    commands = ChatCommands(_cfg(require_code=True), room)
    await room.emit_chat("p1", f"/auth {commands.auth.code}", private=True)
    await room.emit_chat("p1", "/permitir Beltrano")
    await room.emit_chat("p2", "/pular")
    assert commands.skip.is_set()
    # outra pessoa com o mesmo nome (outro id) não herda a permissão
    room.participants["p3"] = "Beltrano Souza"
    await room.emit_chat("p3", "/encerrar")
    assert not commands.end.is_set()


async def test_ajuda_explica_auth_sem_revelar_codigo():
    room = FakeRoom({"p1": "Fulana da Silva"})
    commands = ChatCommands(_cfg(require_code=True), room)
    await room.emit_chat("p1", f"/auth {commands.auth.code}", private=True)
    await room.emit_chat("p1", "/ajuda")
    assert "/auth" in room.sent[-1]
    assert commands.auth.code not in room.sent[-1]


async def test_stealth_recusa_fim_sem_auth():
    room = FakeRoom({"p1": "Fulana da Silva"})
    session = StealthSession(_cfg(require_code=True), room)
    await room.emit_chat("p1", "/fim")
    assert not session.finished.is_set()
    assert room.sent == []  # stealth não responde no chat

    await room.emit_chat("p1", f"/auth {session.auth.code}", private=True)
    await room.emit_chat("p1", "/fim")
    assert session.finished.is_set()


async def test_stealth_sem_codigo_usa_nome_exato():
    room = FakeRoom({"p1": "Fulana Hacker", "p2": "Fulána da Silva"})
    session = StealthSession(_cfg(require_code=False), room)
    await room.emit_chat("p1", "/fim")
    assert not session.finished.is_set()
    await room.emit_chat("p2", "/fim")
    assert session.finished.is_set()
