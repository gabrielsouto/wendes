"""Toca áudio no bot_mic_sink — vira a fala do bot na sala."""
from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)

MIC_SINK = "bot_mic_sink"


async def play_wav(path: str, sink: str = MIC_SINK) -> None:
    """Toca um WAV no sink virtual e espera terminar."""
    proc = await asyncio.create_subprocess_exec(
        "paplay", f"--device={sink}", path,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"paplay falhou: {err.decode(errors='replace')}")
    log.debug("Tocou %s em %s", path, sink)
