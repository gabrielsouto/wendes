"""POST da ata em webhook genérico (Teams/Mattermost/etc.)."""
from __future__ import annotations

import logging

import httpx

from wendes.config import Config

log = logging.getLogger(__name__)


def post_ata(cfg: Config, ata_markdown: str) -> None:
    url = cfg.distribution.webhook_url
    if not url:
        return
    resp = httpx.post(url, json={"text": ata_markdown}, timeout=30)
    resp.raise_for_status()
    log.info("Ata publicada no webhook")
