"""Envio da ata por e-mail (SMTP): markdown em texto puro + HTML alternativo."""
from __future__ import annotations

import html
import logging
import re
import smtplib
from email.message import EmailMessage

import markdown

from wendes.config import Config

log = logging.getLogger(__name__)

# CSS inline mínimo — o Outlook ignora <style> e boa parte do CSS moderno.
_FONT = "font-family: 'Segoe UI', Arial, Helvetica, sans-serif"
_TAG_STYLES = {
    "h1": f"{_FONT}; font-size: 22px; color: #1f2933; margin: 0 0 12px 0",
    "h2": f"{_FONT}; font-size: 18px; color: #1f2933; margin: 20px 0 8px 0;"
    " border-bottom: 1px solid #d9dee3; padding-bottom: 4px",
    "h3": f"{_FONT}; font-size: 15px; color: #1f2933; margin: 16px 0 6px 0",
    "p": f"{_FONT}; font-size: 14px; line-height: 1.5; margin: 0 0 10px 0",
    "ul": f"{_FONT}; font-size: 14px; line-height: 1.5; margin: 0 0 10px 0;"
    " padding-left: 22px",
    "ol": f"{_FONT}; font-size: 14px; line-height: 1.5; margin: 0 0 10px 0;"
    " padding-left: 22px",
    "li": "margin: 0 0 4px 0",
    "table": f"{_FONT}; font-size: 14px; border-collapse: collapse; margin: 0 0 12px 0",
    "th": "border: 1px solid #c5cbd3; padding: 6px 10px; background: #f0f2f5;"
    " text-align: left",
    "td": "border: 1px solid #c5cbd3; padding: 6px 10px; vertical-align: top",
    "blockquote": f"{_FONT}; margin: 0 0 12px 0; padding: 8px 12px;"
    " border-left: 4px solid #e0a800; background: #fff8e1",
    "hr": "border: 0; border-top: 1px solid #d9dee3; margin: 16px 0",
}
_TAG_RE = re.compile(r"<(%s)(\s[^>]*)?>" % "|".join(_TAG_STYLES))


def _inline_style(match: re.Match) -> str:
    tag, attrs = match.group(1), match.group(2) or ""
    style = _TAG_STYLES[tag]
    close = ">"
    if attrs.rstrip().endswith("/"):  # <hr />
        attrs, close = attrs.rstrip()[:-1].rstrip(), " />"
    if 'style="' in attrs:  # ex.: alinhamento de coluna das tabelas
        merged = attrs.replace('style="', 'style="' + style + "; ", 1)
        return f"<{tag}{merged}{close}"
    return f'<{tag}{attrs} style="{style}"{close}'


def ata_html(ata_markdown: str, title: str = "") -> str:
    """Converte a ata em HTML com CSS inline legível no Outlook e no Gmail."""
    body = markdown.markdown(ata_markdown, extensions=["tables", "sane_lists"])
    body = _TAG_RE.sub(_inline_style, body)
    return (
        "<!DOCTYPE html>\n<html><head><meta charset=\"utf-8\">"
        f"<title>{html.escape(title)}</title></head>"
        f'<body style="{_FONT}; color: #1f2933; margin: 0; padding: 16px">'
        f'<div style="max-width: 760px">{body}</div></body></html>\n'
    )


def ata_subject(cfg: Config, date: str) -> str:
    team = cfg.team.name.strip()
    return f"Ata da Daily — {team} — {date}" if team else f"Ata da Daily — {date}"


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
    subject = ata_subject(cfg, date)
    msg["Subject"] = subject
    msg["From"] = d.email_from
    msg["To"] = ", ".join(d.email_to)
    # multipart/alternative: o cliente escolhe entre o markdown e o HTML
    msg.set_content(ata_markdown)
    msg.add_alternative(ata_html(ata_markdown, subject), subtype="html")
    _smtp_send(cfg, msg)
    log.info("Ata enviada para %s", d.email_to)
