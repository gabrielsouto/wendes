"""Envio da ata por e-mail (SMTP)."""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from wendes.config import Config

log = logging.getLogger(__name__)


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
    with smtplib.SMTP(d.smtp_host, d.smtp_port, timeout=30) as smtp:
        password = cfg.secrets.smtp_password
        if password:
            smtp.starttls()
            smtp.login(d.email_from, password)
        smtp.send_message(msg)
    log.info("Ata enviada para %s", d.email_to)
