"""Exportação estática da página da reunião (abre offline, sem servidor).

Gera `page/index.html` autocontido: CSS, JS da página e bibliotecas (marked,
DOMPurify, mermaid) embutidos, e o estado completo em JSON. Nenhum
`<script src>` externo.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from wendes.assistant.state import MeetingState
from wendes.assistant.web.server import STATIC_DIR, page_html

log = logging.getLogger(__name__)

VENDOR = ("marked.min.js", "purify.min.js", "mermaid.min.js")


def _safe_script(text: str) -> str:
    """Evita que o conteúdo feche a tag <script> antes da hora."""
    return re.sub(r"</(script)", r"<\\/\1", text, flags=re.IGNORECASE)


def _state_json(snapshot: dict) -> str:
    raw = json.dumps(snapshot, ensure_ascii=False)
    # </script>, <!-- e separadores de linha JS não podem aparecer crus no HTML
    return (raw.replace("<", "\\u003c").replace(">", "\\u003e")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def render_export(snapshot: dict, static_dir: Path = STATIC_DIR) -> str:
    html = page_html("export", base="__BASE__")
    css = (static_dir / "style.css").read_text(encoding="utf-8")
    html = html.replace('<link rel="stylesheet" href="__BASE__/style.css">',
                        f"<style>\n{css}\n</style>")
    for name in VENDOR:
        tag = f'<script src="__BASE__/vendor/{name}"></script>'
        path = static_dir / "vendor" / name
        if path.exists():
            body = _safe_script(path.read_text(encoding="utf-8"))
            html = html.replace(tag, f"<script>\n{body}\n</script>")
        else:
            log.warning("%s ausente (rode scripts/vendor_js.sh): exportação sem essa biblioteca",
                        name)
            html = html.replace(tag, "")
    app = _safe_script((static_dir / "app.js").read_text(encoding="utf-8"))
    state = f"<script>window.WENDES_STATE = {_state_json(snapshot)};</script>"
    html = html.replace(
        '<script type="module" src="__BASE__/app.js"></script>',
        f'{state}\n<script type="module">\n{app}\n</script>',
    )
    if "__BASE__" in html:  # pragma: no cover — sinal de template alterado
        raise RuntimeError("template da página com referência não embutida")
    return html


def export_page(state: MeetingState, out_dir: Path) -> Path:
    snap = state.snapshot()
    snap["status"] = "finished"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "index.html"
    path.write_text(render_export(snap), encoding="utf-8")
    log.info("Página exportada em %s", path)
    return path
