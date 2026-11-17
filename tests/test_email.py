from __future__ import annotations

import smtplib

import pytest

from conftest import make_cfg
from wendes.distribution.email import ata_html, send_ata

ATA = """# Ata da Daily — 2026-07-03

**Início:** 09:30 · **Sala:** https://meet.jit.si/Sala

## Resumo

Tudo andando.

| Pessoa | Status |
|:-------|--------|
| Fulana | ok     |

## Impedimentos e ações

- revisar PR
- liberar acesso

---
_Ata gerada automaticamente pelo Wendes._
"""


class FakeSMTP:
    sent: list = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        pass

    def login(self, user, password):
        pass

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


@pytest.fixture
def smtp(monkeypatch):
    FakeSMTP.sent = []
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    return FakeSMTP


def _cfg(team: str = "Equipe Teste"):
    return make_cfg(
        team={"name": team},
        distribution={
            "email_enabled": True,
            "smtp_host": "smtp.local",
            "email_from": "bot@exemplo.com",
            "email_to": ["time@exemplo.com"],
        },
    )


def test_email_multipart_com_texto_e_html(smtp):
    send_ata(_cfg(), ATA, "2026-07-03")
    (msg,) = smtp.sent
    assert msg.get_content_type() == "multipart/alternative"
    parts = list(msg.iter_parts())
    assert [p.get_content_type() for p in parts] == ["text/plain", "text/html"]
    assert parts[0].get_content().strip() == ATA.strip()
    html = parts[1].get_content()
    assert "<h1" in html and "<h2" in html
    assert "<table" in html and "border: 1px solid" in html
    assert msg["Subject"] == "Ata da Daily — Equipe Teste — 2026-07-03"


def test_assunto_sem_equipe(smtp):
    send_ata(_cfg(team=""), ATA, "2026-07-03")
    assert smtp.sent[0]["Subject"] == "Ata da Daily — 2026-07-03"


def test_email_desabilitado_nao_envia(smtp):
    cfg = _cfg()
    cfg.distribution.email_enabled = False
    send_ata(cfg, ATA, "2026-07-03")
    assert smtp.sent == []


def test_html_com_estilos_inline_validos():
    html = ata_html(ATA, "t")
    assert '<hr style="' in html and "/ style" not in html
    # coluna alinhada: o estilo é mesclado num único atributo style
    assert 'text-align: left"' in html
    assert html.count("<th ") == html.count("<th style=")
