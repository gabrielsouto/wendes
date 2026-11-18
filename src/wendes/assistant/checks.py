"""Verificações do modo assist para `wendes check` (sem rede, sem navegador)."""
from __future__ import annotations

import importlib.util
import socket

from wendes.assistant.web.export import VENDOR


def port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
        except OSError:
            return False
    return True


def check_assist(cfg, static_dir=None) -> list[tuple[bool, str]]:
    """(ok, mensagem) para cada requisito do modo assist."""
    from wendes.assistant.web.server import STATIC_DIR

    static_dir = static_dir or STATIC_DIR
    out: list[tuple[bool, str]] = []
    have = importlib.util.find_spec("aiohttp") is not None
    out.append((have, "aiohttp instalado" if have else "aiohttp ausente (pip install -e .)"))
    for name in ("index.html", "app.js", "style.css"):
        ok = (static_dir / name).exists()
        out.append((ok, f"página: {name}" + ("" if ok else " ausente")))
    missing = [n for n in VENDOR if not (static_dir / "vendor" / n).exists()]
    out.append((not missing, "JS da página (marked, DOMPurify, mermaid) presentes" if not missing
                else f"faltam {', '.join(missing)} — rode scripts/vendor_js.sh"))
    web = cfg.assistant.web
    free = port_free(web.host, web.port)
    out.append((free, f"porta {web.host}:{web.port} livre" if free
                else f"porta {web.host}:{web.port} ocupada (assistant.web.port)"))
    has_vad = importlib.util.find_spec("faster_whisper") is not None
    out.append((has_vad, "VAD disponível (faster-whisper/silero)" if has_vad
                else "faster-whisper ausente: sem VAD para a transcrição ao vivo"))
    return out
