"""Perguntas (F3): roteamento, montagem de contexto com orçamento e resposta.

Contexto (orçamento derivado de `llm.context_tokens`):
  sistema ≤ 300 tokens · notas ≤ 25% · transcrição recente ≤ 35% ·
  trechos recuperados desta reunião ≤ 15% · histórico (com permissão) ≤ 15% ·
  reserva da resposta = 400. Quando falta espaço, corta primeiro a
  transcrição mais antiga.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field

from wendes.assistant import prompts_ptbr as P
from wendes.assistant.budget import Part, estimate_tokens, fit
from wendes.assistant.llm_queue import ask_json
from wendes.assistant.notes import parse_ts
from wendes.assistant.search import _fold

log = logging.getLogger(__name__)

ANSWER_MAX_TOKENS = 400
SYSTEM_MAX_TOKENS = 300

Intent = Literal[
    "pergunta", "documento", "diagrama", "cronograma", "checklist", "tabela", "resumo", "acoes",
    "outro",
]

_SHORTCUTS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b(cria|crie|criar|faz|faca|fazer|monta|monte|montar|escreve|escreva|gera|"
                r"gere|prepara|prepare)\b.*\b(documento|doc|plano|proposta|rascunho)\b"),
     "documento"),
    (re.compile(r"\b(desenha|desenhe|desenhar|diagrama|fluxo|fluxograma|mapa mental)\b"),
     "diagrama"),
    (re.compile(r"\b(cronograma|gantt|linha do tempo)\b"), "cronograma"),
    (re.compile(r"\b(checklist|lista de tarefas|lista de verificacao)\b"), "checklist"),
    (re.compile(r"\b(tabela)\b.*\b(cria|crie|faz|faca|monta|monte|gera|gere)\b|"
                r"\b(cria|crie|faz|faca|monta|monte|gera|gere)\b.*\btabela\b"), "tabela"),
    (re.compile(r"\b(resume|resuma|resumir|resumo|recap)\b"), "resumo"),
    (re.compile(r"\b(quais (sao|foram) as acoes|lista as acoes|liste as acoes)\b"), "acoes"),
]
_PAST = re.compile(
    r"\b(reuniao (passada|anterior)|ultima reuniao|semana passada|mes passado|"
    r"reunioes anteriores|historico|outra reuniao|na (daily|reuniao) de (ontem|segunda|terca|"
    r"quarta|quinta|sexta)|\d{1,2}/\d{1,2}|\d{4}-\d{2}-\d{2})\b"
)


def route_keywords(text: str) -> str | None:
    folded = _fold(text)
    for pattern, intent in _SHORTCUTS:
        if pattern.search(folded):
            return intent
    return None


def mentions_past(text: str) -> bool:
    return bool(_PAST.search(_fold(text)))


class RouterOut(BaseModel):
    intencao: Intent = "pergunta"


class Citation(BaseModel):
    ts: Any = None
    data: str | None = None


class QAOutput(BaseModel):
    resposta_curta: str = ""
    resposta: str
    citacoes: list[Citation] = Field(default_factory=list)


@dataclass
class QAContext:
    notes: list[str] = field(default_factory=list)
    recent: list[str] = field(default_factory=list)
    retrieved: list[str] = field(default_factory=list)
    history: list[str] = field(default_factory=list)


DEFAULT_SHARES = {"notes": 0.25, "recent": 0.35, "retrieved": 0.15, "history": 0.15}


def budget_parts(ctx: QAContext, context_tokens: int, system: str, question: str,
                 shares: dict[str, float] | None = None) -> dict:
    """Corta cada parte no seu teto e o total no que sobra do contexto."""
    sh = {**DEFAULT_SHARES, **(shares or {})}
    total = context_tokens - ANSWER_MAX_TOKENS - min(estimate_tokens(system), SYSTEM_MAX_TOKENS)
    total -= estimate_tokens(P.QA) + estimate_tokens(question) + 50
    total = max(300, total)
    parts = [
        Part("recent", ctx.recent, int(context_tokens * sh["recent"]), keep="newest",
             cut_order=0),
        Part("retrieved", ctx.retrieved, int(context_tokens * sh["retrieved"]), keep="first",
             cut_order=1),
        Part("history", ctx.history, int(context_tokens * sh["history"]), keep="first",
             cut_order=2),
        Part("notes", ctx.notes, int(context_tokens * sh["notes"]), keep="first", cut_order=3),
    ]
    return fit(parts, total)


def build_prompt(question: str, who: str, ctx: QAContext, context_tokens: int,
                 system: str, shares: dict[str, float] | None = None) -> str:
    parts = budget_parts(ctx, context_tokens, system, question, shares)
    return P.QA.format(
        quem=who or "um participante",
        pergunta=question,
        notas="\n".join(parts["notes"]) or "(sem notas ainda)",
        recente="\n".join(parts["recent"]) or "(sem transcrição recente)",
        recuperado="\n".join(parts["retrieved"]) or "(nenhum)",
        historico="\n".join(parts["history"]) or "(não consultado)",
    )


def clean_citations(out: QAOutput, max_ts: float | None) -> list[dict]:
    cites: list[dict] = []
    for c in out.citacoes:
        if c.data:
            ts = parse_ts(c.ts)
            cites.append({"data": str(c.data)[:10], "ts": ts or 0})
            continue
        ts = parse_ts(c.ts, max_ts)
        if ts is not None and {"ts": ts} not in cites:
            cites.append({"ts": ts})
    return cites


def answer(llm, question: str, who: str, ctx: QAContext, *, context_tokens: int, system: str,
           mode: str = "json_object", vllm_key: str = "guided_json",
           shares: dict[str, float] | None = None) -> QAOutput | None:
    prompt = build_prompt(question, who, ctx, context_tokens, system, shares)
    return ask_json(llm, prompt, QAOutput, system=system, mode=mode, vllm_key=vllm_key,
                    max_tokens=ANSWER_MAX_TOKENS)


def route_llm(llm, text: str, *, system: str, mode: str = "json_object",
              vllm_key: str = "guided_json") -> str:
    try:
        out = ask_json(llm, P.ROUTER.format(pedido=text), RouterOut, system=system, mode=mode,
                       vllm_key=vllm_key, max_tokens=30)
    except Exception:  # noqa: BLE001 — sem roteador, é pergunta
        return "pergunta"
    return out.intencao if out is not None else "pergunta"


def history_context(db, question: str, *, exclude_key: str | None = None,
                    keys: list[str] | None = None, k: int = 4) -> list[str]:
    """Trechos de reuniões anteriores, sempre com a data."""
    from wendes.assistant.search import search_atas, search_history

    lines: list[str] = []
    for hit in search_history(db, question, k, keys=keys, exclude_key=exclude_key):
        lines.append(f"Reunião de {hit.meeting_key}:")
        lines.extend(f"  {ln}" for ln in hit.lines)
    for key, snip in search_atas(db, question, 2, exclude_key=exclude_key):
        if keys and key not in keys:
            continue
        lines.append(f"Ata de {key}: {snip}")
    return lines


def short_question(text: str, limit: int = 60) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# ---- depois da reunião (wendes ask) ------------------------------------------------
def offline_answer(cfg, llm, store, question: str, date: str | None = None,
                   hora: str | None = None) -> tuple[QAOutput | None, str]:
    """Mesmo motor das perguntas ao vivo, sem Jitsi. Retorna (resposta, escopo)."""
    from wendes.assistant.finalize import load_notes
    from wendes.assistant.notes import full_view
    from wendes.assistant.search import meeting_key, open_db, search_segments, segments_from_run
    from wendes.assistant.state import Segment

    system = P.system(cfg.room.bot_display_name, cfg.team.name)
    data_dir = cfg.resolve(cfg.data_dir)
    if date:
        record = store.load_record(date, hora)
        if record is None:
            return None, f"nenhuma reunião em {date} {hora or ''}".strip()
        run_dir = store.run_dir(date, record.started_at)
        segs = [
            Segment(i + 1, ts, ts, text, speaker)
            for i, (ts, speaker, text) in enumerate(segments_from_run(run_dir))
        ]
        blocks = search_segments(segs, question, k=6)
        ctx = QAContext(
            notes=full_view(load_notes(run_dir)),
            recent=[s.line("não identificado") for s in segs],
            retrieved=[s.line("não identificado") for b in blocks for s in b],
        )
        shares = {"notes": 0.3, "recent": 0.3, "retrieved": 0.3, "history": 0.0}
        scope = f"reunião de {meeting_key(date, record.started_at)}"
    else:
        db = open_db(data_dir)
        try:
            ctx = QAContext(history=history_context(db, question, k=8))
        finally:
            db.close()
        shares = {"notes": 0.0, "recent": 0.0, "retrieved": 0.0, "history": 0.75}
        scope = "todo o histórico"
    out = answer(
        llm, question, "você", ctx, context_tokens=cfg.llm.context_tokens, system=system,
        mode=cfg.llm.structured_output, vllm_key=cfg.llm.vllm_guided_key, shares=shares,
    )
    return out, scope
