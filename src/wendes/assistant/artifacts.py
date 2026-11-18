"""Artefatos ao vivo (F4): documentos, diagramas, cronogramas, checklists e tabelas.

Cada artefato tem versões em `artifacts/{id}-{slug}/v{n}.md` (ou `.mmd`) e um
índice em `artifacts/artifacts.json`. Cada versão registra o autor (Wendes ou
quem editou na página) e o pedido que a gerou. Revisões do LLM sempre partem da
versão mais recente, inclusive das edições humanas.
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel
from rapidfuzz import fuzz

log = logging.getLogger(__name__)

FORMATS = {"doc": "md", "checklist": "md", "tabela": "md", "diagrama": "mmd", "cronograma": "mmd"}
INTENT_TO_TYPE = {"documento": "doc", "doc": "doc", "diagrama": "diagrama",
                  "cronograma": "cronograma", "checklist": "checklist", "tabela": "tabela"}
MERMAID_STARTS = ("flowchart", "graph", "sequenceDiagram", "gantt", "mindmap", "timeline",
                  "stateDiagram-v2", "stateDiagram")
MAX_CHARS = 1500 * 4  # ~1500 tokens
TITLE_MATCH = 80
_FENCE = re.compile(r"^\s*```[\w-]*\s*\n(.*?)\n\s*```\s*$", re.DOTALL)
UPDATE_VERBS = re.compile(
    r"\b(atualiza|atualize|altera|altere|acrescenta|acrescente|adiciona|adicione|muda|mude|"
    r"corrige|corrija|inclui|inclua|tira|tire|remove|remova|edita|edite)\b"
)


class ArtifactOut(BaseModel):
    tipo: str = ""
    titulo: str
    conteudo: str


def strip_fences(text: str) -> str:
    text = text.strip()
    m = _FENCE.match(text)
    if m:
        return m.group(1).strip()
    if text.startswith("```"):  # cerca sem fechamento
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    return text.strip()


def validate(tipo: str, conteudo: str) -> str:
    """Conteúdo limpo; ValueError com o motivo se for inválido."""
    if tipo not in FORMATS:
        raise ValueError(f"tipo desconhecido: {tipo}")
    text = strip_fences(conteudo or "")
    if not text.strip():
        raise ValueError("conteúdo vazio")
    if len(text) > MAX_CHARS * 2:
        raise ValueError("conteúdo grande demais")
    if FORMATS[tipo] == "mmd":
        first = next((ln.strip() for ln in text.splitlines() if ln.strip()
                      and not ln.strip().startswith("%%")), "")
        if not first.startswith(MERMAID_STARTS):
            raise ValueError(
                "a primeira linha do Mermaid precisa começar com " + ", ".join(MERMAID_STARTS)
            )
        if tipo == "cronograma" and not first.startswith(("gantt", "timeline")):
            raise ValueError("cronograma precisa ser um Mermaid gantt")
    return text


def slugify(text: str) -> str:
    dec = unicodedata.normalize("NFKD", text.casefold())
    ascii_ = "".join(c for c in dec if not unicodedata.combining(c))
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_).strip("-")
    return slug[:40] or "artefato"


@dataclass
class Version:
    n: int
    autor: str
    pedido: str
    criado_em: str
    arquivo: str
    conteudo: str = ""
    reparo: bool = False


@dataclass
class Artifact:
    id: int
    tipo: str
    titulo: str
    slug: str
    formato: str
    versoes: list[Version] = field(default_factory=list)

    @property
    def latest(self) -> Version:
        return self.versoes[-1]

    def public(self) -> dict:
        d = asdict(self)
        d["arquivo"] = self.latest.arquivo if self.versoes else ""
        return d


class ArtifactStore:
    def __init__(self, run_dir: Path | None) -> None:
        self.dir = run_dir / "artifacts" if run_dir is not None else None
        self.items: list[Artifact] = []
        self.load()

    def load(self) -> None:
        if self.dir is None:
            return
        index = self.dir / "artifacts.json"
        if not index.exists():
            return
        try:
            data = json.loads(index.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.warning("artifacts.json ilegível")
            return
        for a in data:
            versions = [Version(**{k: v for k, v in x.items() if k in Version.__dataclass_fields__})
                        for x in a.get("versoes", [])]
            self.items.append(Artifact(a["id"], a["tipo"], a["titulo"], a["slug"], a["formato"],
                                       versions))

    def _save_index(self) -> None:
        if self.dir is None:
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.dir / "artifacts.json.tmp"
        tmp.write_text(json.dumps(self.public(), ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.dir / "artifacts.json")

    def _write_version(self, art: Artifact, v: Version) -> None:
        if self.dir is None:
            return
        folder = self.dir / f"{art.id}-{art.slug}"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"v{v.n}.{art.formato}").write_text(v.conteudo + "\n", encoding="utf-8")

    def public(self) -> list[dict]:
        return [a.public() for a in self.items]

    def get(self, aid: int) -> Artifact | None:
        return next((a for a in self.items if a.id == aid), None)

    def find_by_title(self, text: str) -> Artifact | None:
        best, score = None, 0.0
        folded = slugify(text).replace("-", " ")
        for a in self.items:
            s = fuzz.partial_ratio(slugify(a.titulo).replace("-", " "), folded)
            if s > score:
                best, score = a, s
        return best if score >= TITLE_MATCH else None

    def create(self, tipo: str, titulo: str, conteudo: str, *, autor: str, pedido: str) -> Artifact:
        aid = max((a.id for a in self.items), default=0) + 1
        art = Artifact(aid, tipo, titulo.strip()[:80] or "Sem título", slugify(titulo),
                       FORMATS[tipo])
        self.items.append(art)
        self.add_version(aid, conteudo, autor=autor, pedido=pedido)
        return art

    def add_version(self, aid: int, conteudo: str, *, autor: str, pedido: str,
                    titulo: str | None = None, reparo: bool = False) -> Version:
        art = self.get(aid)
        if art is None:
            raise KeyError(aid)
        n = len(art.versoes) + 1
        if titulo and titulo.strip():
            art.titulo = titulo.strip()[:80]
        v = Version(
            n=n, autor=autor[:60], pedido=pedido[:300],
            criado_em=datetime.now().isoformat(timespec="seconds"),
            arquivo=f"artifacts/{art.id}-{art.slug}/v{n}.{art.formato}",
            conteudo=conteudo, reparo=reparo,
        )
        art.versoes.append(v)
        self._write_version(art, v)
        self._save_index()
        return v

    def latest_files(self) -> list[Path]:
        """Arquivos da versão mais recente de cada artefato (anexos do e-mail)."""
        if self.dir is None:
            return []
        run_dir = self.dir.parent
        return [run_dir / a.latest.arquivo for a in self.items if a.versoes
                and (run_dir / a.latest.arquivo).exists()]
