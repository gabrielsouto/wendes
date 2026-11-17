"""Envio da ata por e-mail (SMTP)."""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from wendes.config import Config

log = logging.getLogger(__name__)


def _smtp_send(cfg: Config, msg: EmailMessage) -> None:
    d = cfg.distribution
    with smtplib.SMTP(d.smtp_host, d.smtp_port, timeout=30) as smtp:
        password = cfg.secrets.smtp_password
        if password:
            smtp.starttls()
            smtp.login(d.email_from, password)
        smtp.send_message(msg)


def send_admin_code(cfg: Config, code: str) -> None:
    """E-mail com o código de admin da execução para commands.admin_emails."""
    d = cfg.distribution
    msg = EmailMessage()
    team = f" — {cfg.team.name}" if cfg.team.name else ""
    msg["Subject"] = f"Wendes{team}: código de admin da reunião"
    msg["From"] = d.email_from
    msg["To"] = ", ".join(cfg.commands.admin_emails)
    msg.set_content(
        f"Código de admin desta reunião: {code}\n\n"
        "Para usar os comandos do chat, envie /auth <código> por MENSAGEM PRIVADA"
        " ao bot no Jitsi. Nunca mande o código no chat público (ele é trocado).\n"
    )
    _smtp_send(cfg, msg)
    log.info("Código de admin enviado para %s", cfg.commands.admin_emails)


def send_ata(cfg: Config, ata_markdown: str, date: str) -> None:
    d = cfg.distribution
    if not d.email_enabled or not d.email_to:
        log.info("E-mail desabilitado — pulando envio")
        return
    msg = EmailMessage()
    msg["Subject"] = f"Ata da Daily — {date}"
    msg["From"] = d.email_from
    msg["To"] = ", ".join(d.email_to)
    msg.set_content(ata_markdown)
    _smtp_send(cfg, msg)
    log.info("Ata enviada para %s", d.email_to)
