"""Garante que os dispositivos virtuais de áudio existem."""
from __future__ import annotations

import subprocess

from wendes.config import PROJECT_ROOT

REQUIRED_SOURCES = ("bot_mic", "bot_out.monitor")
REQUIRED_SINKS = ("bot_mic_sink", "bot_out")


def ensure_devices() -> None:
    sinks = subprocess.run(
        ["pactl", "list", "short", "sinks"], capture_output=True, text=True, check=True
    ).stdout
    sources = subprocess.run(
        ["pactl", "list", "short", "sources"], capture_output=True, text=True, check=True
    ).stdout
    if all(s in sinks for s in REQUIRED_SINKS) and all(s in sources for s in REQUIRED_SOURCES):
        return
    subprocess.run([str(PROJECT_ROOT / "scripts" / "setup_audio.sh")], check=True)
