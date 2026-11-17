"""Notas vivas (F2): atualização incremental por operações e compactação.

O LLM recebe as notas atuais compactadas e só os segmentos novos, e devolve
OPERAÇÕES (`add_point`, `add_decision`, `add_action`, `update_action`,
`add_open_question`). O Python aplica de forma determinística: nenhuma
operação apaga conteúdo, operações inválidas são descartadas com log, e um
tópico existente é reaproveitado quando o título casa com fuzzy ≥ 90. Assim a
saída fica curta e o modelo não consegue "perder" notas anteriores.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import unicodedata
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from rapidfuzz import fuzz

from wendes.assistant import prompts_ptbr as P
from wendes.assistant.budget import Part, estimate_tokens, fit
from wendes.assistant.llm_queue import BACKGROUND, NOTES, ask_json, est
from wendes.assistant.state import MeetingState, Segment, append_jsonl, mmss

log = logging.getLogger(__name__)

TOPIC_MATCH = 90
DUPLICATE_MATCH = 95
OPS_MAX_TOKENS = 700
COMPACT_MIN_POINTS = 4


# ---- modelos ------------------------------------------------------------------
class Point(BaseModel):
    texto: str
    ts: float | None = None


class Topic(BaseModel):
    id: str
    titulo: str
    pontos: list[Point] = []


class Decision(BaseModel):
    id: str
    texto: str
    ts: float | None = None


class ActionItem(BaseModel):
    id: str
    descricao: str
    responsavel: str = ""  # "" = não identificado
    prazo: str = ""
    ts: float | None = None
    status: Literal["aberta", "concluida", "cancelada"] = "aberta"
    origem: Literal["reuniao", "anterior"] = "reuniao"


class PrevPending(BaseModel):
    """Pendência aberta de uma reunião anterior (id P1…Pn nesta reunião)."""

    id: str
    descricao: str
    responsavel: str = ""
    db_id: int | None = None
    meeting_date: str = ""
    status: Literal["sem_mencao", "mencionada", "concluida", "cancelada"] = "sem_mencao"
    evidencia_ts: float | None = None
    evidencia: str = ""


class OpenQuestion(BaseModel):
    texto: str
    ts: float | None = None


class Notes(BaseModel):
    topicos: list[Topic] = []
    decisoes: list[Decision] = []
    acoes: list[ActionItem] = []
    pendencias_anteriores: list[PrevPending] = []
    perguntas_abertas: list[OpenQuestion] = []


class Op(BaseModel):
    model_config = ConfigDict(extra="allow")
    op: str
    topico: str | None = None
    texto: str | None = None
    descricao: str | None = None
    responsavel: str | None = None
    prazo: str | None = None
    id: str | None = None
    status: str | None = None
    ts: Any = None
    evidencia_ts: Any = None


class OpsResponse(BaseModel):
    ops: list[Op] = []


class CompactPoint(BaseModel):
    texto: str
    ts: Any = None
    de: list[int] = Field(default_factory=list)


class CompactResponse(BaseModel):
    pontos: list[CompactPoint]


# ---- utilitários -----------------------------------------------------------------
def _norm(text: str) -> str:
    dec = unicodedata.normalize("NFKD", (text or "").casefold())
    return " ".join("".join(c for c in dec if not unicodedata.combining(c)).split())


_MMSS = re.compile(r"^\s*\[?(?:(\d+):)?(\d{1,2}):(\d{2})\]?\s*$")


def parse_ts(value: Any, max_ts: float | None = None) -> float | None:
    """Aceita segundos (812) ou "mm:ss"/"h:mm:ss"; None se inválido ou fora da reunião."""
    if value is None or value == "":
        return None
    t: float | None = None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        t = float(value)
    elif isinstance(value, str):
        m = _MMSS.match(value)
        if m:
            t = int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
        else:
            try:
                t = float(value)
            except ValueError:
                return None
    if t is None or t < 0 or (max_ts is not None and t > max_ts + 5):
        return None
    return round(t, 1)


def _next_id(prefix: str, existing: list[str]) -> str:
    nums = [
        int(x[len(prefix):]) for x in existing
        if x.startswith(prefix) and x[len(prefix):].isdigit()
    ]
    return f"{prefix}{max(nums, default=0) + 1}"


def _is_dup(text: str, others: list[str]) -> bool:
    n = _norm(text)
    return any(fuzz.ratio(n, _norm(o)) >= DUPLICATE_MATCH for o in others)


def find_topic(notes: Notes, title: str) -> Topic | None:
    n = _norm(title)
    best, score = None, 0.0
    for t in notes.topicos:
        s = fuzz.token_sort_ratio(n, _norm(t.titulo))
        if s > score:
            best, score = t, s
    return best if score >= TOPIC_MATCH else None


def owner_ok(name: str, allowed: set[str]) -> str:
    """Responsável só se bater (fuzzy ≥ 85) com um nome permitido; senão ""."""
    name = (name or "").strip()
    if not name or not allowed:
        return ""
    n = _norm(name)
    for cand in allowed:
        c = _norm(cand)
        if fuzz.ratio(n, c) >= 85 or (len(n) >= 3 and fuzz.partial_ratio(n, c) >= 95):
            return cand
    return ""


# ---- aplicação determinística ---------------------------------------------------
def apply_ops(
    notes: Notes,
    ops: list[Op],
    *,
    max_ts: float | None = None,
    allowed_owners: set[str] | None = None,
) -> tuple[list[dict], list[tuple[dict, str]]]:
    """Aplica as operações; retorna (aplicadas, [(rejeitada, motivo)]). Nada é apagado."""
    applied: list[dict] = []
    rejected: list[tuple[dict, str]] = []
    allowed = allowed_owners or set()

    def reject(op: Op, why: str) -> None:
        rejected.append((op.model_dump(exclude_none=True), why))

    for op in ops:
        kind = (op.op or "").strip()
        ts = parse_ts(op.ts, max_ts)
        if kind == "add_point":
            topic_title = (op.topico or "").strip()
            text = (op.texto or "").strip()
            if not topic_title or not text:
                reject(op, "tópico ou texto vazio")
                continue
            topic = find_topic(notes, topic_title)
            if topic is None:
                topic = Topic(id=_next_id("T", [t.id for t in notes.topicos]), titulo=topic_title)
                notes.topicos.append(topic)
            if _is_dup(text, [p.texto for p in topic.pontos]):
                reject(op, "ponto duplicado")
                continue
            topic.pontos.append(Point(texto=text, ts=ts))
            applied.append({"op": kind, "topico": topic.titulo, "texto": text, "ts": ts})
        elif kind == "add_decision":
            text = (op.texto or "").strip()
            if not text:
                reject(op, "texto vazio")
                continue
            if _is_dup(text, [d.texto for d in notes.decisoes]):
                reject(op, "decisão duplicada")
                continue
            d = Decision(id=_next_id("D", [d.id for d in notes.decisoes]), texto=text, ts=ts)
            notes.decisoes.append(d)
            applied.append({"op": kind, "id": d.id, "texto": text, "ts": ts})
        elif kind == "add_action":
            desc = (op.descricao or op.texto or "").strip()
            if not desc:
                reject(op, "descrição vazia")
                continue
            if _is_dup(desc, [a.descricao for a in notes.acoes]):
                reject(op, "ação duplicada")
                continue
            a = ActionItem(
                id=_next_id("A", [a.id for a in notes.acoes]),
                descricao=desc,
                responsavel=owner_ok(op.responsavel or "", allowed),
                prazo=(op.prazo or "").strip()[:60],
                ts=ts,
            )
            notes.acoes.append(a)
            applied.append({"op": kind, **a.model_dump()})
        elif kind == "update_action":
            ref = (op.id or "").strip().upper()
            status = _norm(op.status or "")
            status = {"resolvida": "concluida", "feita": "concluida"}.get(status, status)
            ev_ts = parse_ts(op.evidencia_ts, max_ts) or ts
            target_a = next((a for a in notes.acoes if a.id == ref), None)
            target_p = next((p for p in notes.pendencias_anteriores if p.id == ref), None)
            if target_a is not None and status in ("aberta", "concluida", "cancelada"):
                target_a.status = status  # type: ignore[assignment]
                applied.append({"op": kind, "id": ref, "status": status, "evidencia_ts": ev_ts})
            elif target_p is not None and status in ("mencionada", "concluida", "cancelada"):
                if target_p.status in ("concluida", "cancelada") and status == "mencionada":
                    reject(op, "pendência já resolvida")
                    continue
                target_p.status = status  # type: ignore[assignment]
                target_p.evidencia_ts = ev_ts
                applied.append({"op": kind, "id": ref, "status": status, "evidencia_ts": ev_ts})
            else:
                reject(op, "id ou status inválido")
        elif kind == "add_open_question":
            text = (op.texto or "").strip()
            if not text:
                reject(op, "texto vazio")
                continue
            if _is_dup(text, [q.texto for q in notes.perguntas_abertas]):
                reject(op, "pergunta duplicada")
                continue
            notes.perguntas_abertas.append(OpenQuestion(texto=text, ts=ts))
            applied.append({"op": kind, "texto": text, "ts": ts})
        else:
            reject(op, "operação desconhecida")
    for op_dict, why in rejected:
        log.info("Operação de notas descartada (%s): %s", why, op_dict.get("op"))
    return applied, rejected


def compact_view(notes: Notes, last_points: int = 3) -> list[str]:
    """Notas resumidas para os prompts: tópicos com os últimos pontos, decisões,
    ações abertas e pendências anteriores (com ids)."""
    lines: list[str] = []
    for t in notes.topicos:
        lines.append(f"# {t.titulo}")
        lines.extend(f"- {p.texto}" for p in t.pontos[-last_points:])
    if notes.decisoes:
        lines.append("# Decisões")
        lines.extend(f"- {d.texto}" for d in notes.decisoes)
    abertas = [a for a in notes.acoes if a.status == "aberta"]
    if abertas:
        lines.append("# Ações abertas")
        lines.extend(
            f"- {a.id}: {a.descricao}"
            + (f" (resp.: {a.responsavel})" if a.responsavel else "")
            + (f" (prazo: {a.prazo})" if a.prazo else "")
            for a in abertas
        )
    if notes.pendencias_anteriores:
        lines.append("# Pendências de reuniões anteriores")
        lines.extend(
            f"- {p.id}: {p.descricao} [{p.status}]" for p in notes.pendencias_anteriores
        )
    return lines


def full_view(notes: Notes) -> list[str]:
    """Notas completas (resumo, ata)."""
    lines: list[str] = []
    for t in notes.topicos:
        lines.append(f"# {t.titulo}")
        lines.extend(f"- {p.texto}" for p in t.pontos)
    if notes.decisoes:
        lines.append("# Decisões")
        lines.extend(f"- {d.texto}" for d in notes.decisoes)
    if notes.acoes:
        lines.append("# Ações")
        lines.extend(
            f"- {a.id}: {a.descricao} — {a.responsavel or 'sem responsável'}"
            + (f", prazo {a.prazo}" if a.prazo else "") + f" [{a.status}]"
            for a in notes.acoes
        )
    if notes.pendencias_anteriores:
        lines.append("# Pendências anteriores")
        lines.extend(f"- {p.id}: {p.descricao} [{p.status}]" for p in notes.pendencias_anteriores)
    if notes.perguntas_abertas:
        lines.append("# Perguntas em aberto")
        lines.extend(f"- {q.texto}" for q in notes.perguntas_abertas)
    return lines


def recap_fallback(notes: Notes) -> str:
    """Recap sem LLM, direto das notas."""
    out: list[str] = []
    if notes.topicos:
        out.append("**Tópicos:** " + "; ".join(t.titulo for t in notes.topicos))
    if notes.decisoes:
        out.append("**Decisões:** " + "; ".join(d.texto for d in notes.decisoes[-5:]))
    acts = [a for a in notes.acoes if a.status == "aberta"]
    if acts:
        out.append("**Ações:** " + "; ".join(
            f"{a.descricao} ({a.responsavel or 'sem responsável'})" for a in acts[-6:]))
    return "\n".join(out) or "Ainda não há notas suficientes para um resumo."


def actions_text(notes: Notes) -> str:
    if not notes.acoes:
        return "Nenhuma ação registrada até agora."
    lines = ["Ações:"]
    for a in notes.acoes:
        extra = f", prazo {a.prazo}" if a.prazo else ""
        mark = {"concluida": " ✔", "cancelada": " ✘"}.get(a.status, "")
        lines.append(f"{a.id}. {a.descricao} — {a.responsavel or 'sem responsável'}{extra}{mark}")
    return "\n".join(lines)


def validate_compaction(
    snapshot: list[Point], current: list[Point], result: CompactResponse, max_ts: float | None
) -> list[Point] | None:
    """Novos pontos do tópico, ou None se a compactação perderia conteúdo."""
    n = len(snapshot)
    if n == 0 or [p.texto for p in current[:n]] != [p.texto for p in snapshot]:
        return None  # o tópico mudou de um jeito inesperado
    covered: set[int] = set()
    new: list[Point] = []
    for cp in result.pontos:
        idx = {i - 1 for i in cp.de}
        if not cp.texto.strip() or not idx or any(i < 0 or i >= n for i in idx):
            return None
        covered |= idx
        ts = parse_ts(cp.ts, max_ts)
        if ts is None:
            ts = min((snapshot[i].ts for i in idx if snapshot[i].ts is not None), default=None)
        new.append(Point(texto=cp.texto.strip(), ts=ts))
    if covered != set(range(n)) or len(new) > n:
        return None
    return new + current[n:]


# ---- atualizador ------------------------------------------------------------------
class NotesUpdater:
    def __init__(
        self,
        state: MeetingState,
        scheduler,
        llm,
        *,
        system: str,
        context_tokens: int = 8192,
        interval_s: float = 120.0,
        min_new_chars: int = 1500,
        compact_every_s: float = 900.0,
        structured_mode: str = "json_object",
        vllm_key: str = "guided_json",
        owner_names: set[str] | None = None,
        min_conf: float = 0.6,
        on_change=None,
    ) -> None:
        self.state = state
        self.scheduler = scheduler
        self.llm = llm
        self.system = system
        self.context_tokens = context_tokens
        self.interval_s = interval_s
        self.min_new_chars = min_new_chars
        self.compact_every_s = compact_every_s
        self.mode = structured_mode
        self.vllm_key = vllm_key
        self.owner_names = owner_names or set()
        self.min_conf = min_conf
        self.on_change = on_change
        self.notes = Notes()
        self.last_seg_id = 0
        self.rounds = 0
        self.failures = 0
        self._chars = 0
        self._queued = False
        self._lock = asyncio.Lock()
        self._compact_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        self._last_round = time.monotonic()
        self._last_compact = time.monotonic()
        self.load()

    # ---- persistência --------------------------------------------------
    def load(self) -> None:
        if not self.state.persist:
            return
        path = self.state.path("notes.json")
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self.notes = Notes.model_validate(data)
                self.last_seg_id = int(data.get("_ultimo_segmento", 0))
            except (ValueError, json.JSONDecodeError):
                log.warning("notes.json ilegível — começando notas novas")

    def save(self, ops: list[dict] | None = None) -> None:
        data = self.notes.model_dump()
        if self.state.persist:
            path: Path = self.state.path("notes.json")
            tmp = path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps({**data, "_ultimo_segmento": self.last_seg_id}, ensure_ascii=False,
                           indent=1),
                encoding="utf-8",
            )
            tmp.replace(path)
            for op in ops or []:
                row = {"t": round(time.time(), 1), **op}
                append_jsonl(self.state.path("notes_ops.jsonl"), row)
        self.state.set_notes(data)
        if self.on_change is not None:
            self.on_change(self.notes)

    def set_prev_pending(self, items: list[PrevPending]) -> None:
        self.notes.pendencias_anteriores = items
        self.save()

    # ---- gatilhos ------------------------------------------------------
    async def on_segment(self, seg: Segment) -> None:
        if seg.marker or seg.source == "bot":
            return
        self._chars += len(seg.text)
        if self._chars >= self.min_new_chars:
            self.trigger()

    def has_new(self) -> bool:
        return any(s.id > self.last_seg_id for s in self.state.segments if not s.marker)

    def trigger(self) -> None:
        """Agenda uma rodada; se já há uma na fila, ela só pega a janela maior."""
        if self.llm is None or self._queued:
            return
        self._queued = True
        task = asyncio.create_task(self._round())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def loop(self) -> None:
        while True:
            await asyncio.sleep(min(self.interval_s, 10.0))
            now = time.monotonic()
            if now - self._last_round >= self.interval_s and self.has_new():
                self.trigger()
            if self.compact_every_s and now - self._last_compact >= self.compact_every_s:
                self._last_compact = now
                task = asyncio.create_task(self.compact())
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)

    # ---- rodada incremental ------------------------------------------
    def _owners(self, segs: list[Segment]) -> set[str]:
        allowed: set[str] = set()
        joined = _norm(" ".join(s.text for s in segs))
        for name in self.owner_names:
            first = _norm(name).split(" ")[0] if name else ""
            if first and re.search(rf"\b{re.escape(first)}\b", joined):
                allowed.add(name)
        for s in segs:
            if s.speaker and s.speaker_conf >= self.min_conf:
                allowed.add(s.speaker)
        return allowed

    def build_prompt(self, segs: list[Segment]) -> tuple[str, list[Segment]]:
        """Prompt com orçamento; se os segmentos novos não couberem, pega os mais
        antigos que couberem (o resto fica para a próxima rodada)."""
        reserve = OPS_MAX_TOKENS + estimate_tokens(self.system) + 400
        budget = max(500, self.context_tokens - reserve)
        notes_lines = fit([Part("n", compact_view(self.notes), int(budget * 0.35))], budget)["n"]
        seg_budget = budget - sum(estimate_tokens(x) + 1 for x in notes_lines)
        chosen: list[Segment] = []
        used = 0
        for s in segs:
            cost = estimate_tokens(s.line("não identificado")) + 1
            if chosen and used + cost > seg_budget:
                break
            chosen.append(s)
            used += cost
        prompt = P.NOTES_OPS.format(
            notas="\n".join(notes_lines) or "(ainda sem notas)",
            trechos="\n".join(s.line("não identificado") for s in chosen),
        )
        return prompt, chosen

    def _ask_ops(self) -> tuple[OpsResponse | None, list[Segment]]:
        """Roda na thread do scheduler: pega a janela no momento em que começa."""
        self._queued = False
        segs = [s for s in list(self.state.segments) if s.id > self.last_seg_id and not s.marker]
        if not segs:
            return OpsResponse(), []
        prompt, chosen = self.build_prompt(segs)
        resp = ask_json(
            self.llm, prompt, OpsResponse, system=self.system, mode=self.mode,
            vllm_key=self.vllm_key, max_tokens=OPS_MAX_TOKENS,
        )
        return resp, chosen

    async def _round(self) -> None:
        async with self._lock:
            fut = self.scheduler.submit(NOTES, "notas", self._ask_ops, key="notas",
                                        est_tokens=self.context_tokens // 2)
            try:
                resp, chosen = await fut
            except asyncio.CancelledError:
                self._queued = False
                raise
            except Exception:  # noqa: BLE001 — LLM fora: a transcrição segue
                self._queued = False
                self.failures += 1
                return
            self._last_round = time.monotonic()
            if not chosen:
                return
            if resp is None:
                self.failures += 1
                log.warning("Notas: o LLM não devolveu operações válidas — rodada pulada")
                return
            self.apply(resp.ops, chosen)
            remaining = [s for s in self.state.segments if s.id > self.last_seg_id and not s.marker]
            if sum(len(s.text) for s in remaining) >= self.min_new_chars:
                self.trigger()

    def apply(self, ops: list[Op], chosen: list[Segment]) -> list[dict]:
        max_ts = max((s.end for s in self.state.segments), default=None)
        applied, rejected = apply_ops(
            self.notes, ops, max_ts=max_ts, allowed_owners=self._owners(chosen)
        )
        self.last_seg_id = max(self.last_seg_id, max(s.id for s in chosen))
        self._chars = sum(len(s.text) for s in self.state.text_since(self.last_seg_id))
        self.rounds += 1
        log.info(
            "Notas: %d operação(ões) aplicada(s), %d descartada(s)", len(applied), len(rejected)
        )
        self.save(applied)
        return applied

    async def final_round(self, timeout: float = 180.0) -> None:
        """No encerramento: processa o que sobrou da transcrição ao vivo."""
        if self.llm is None:
            return
        try:
            for _ in range(5):
                if not self.has_new():
                    break
                self._queued = True
                await asyncio.wait_for(self._round(), timeout)
        except Exception:  # noqa: BLE001
            log.warning("Rodada final das notas falhou — seguindo com as notas atuais",
                        exc_info=True)

    # ---- compactação --------------------------------------------------
    async def compact(self) -> int:
        """Funde pontos duplicados em cada tópico grande; retorna quantos tópicos mudaram."""
        if self.llm is None:
            return 0
        changed = 0
        async with self._compact_lock:
            for topic in list(self.notes.topicos):
                if len(topic.pontos) < COMPACT_MIN_POINTS:
                    continue
                snapshot = [p.model_copy() for p in topic.pontos]
                prompt = P.NOTES_COMPACT.format(
                    titulo=topic.titulo,
                    pontos="\n".join(
                        f"{i}. {p.texto}" + (f" [{mmss(p.ts)}]" if p.ts is not None else "")
                        for i, p in enumerate(snapshot, 1)
                    ),
                )

                def call(prompt=prompt):
                    return ask_json(
                        self.llm, prompt, CompactResponse, system=self.system, mode=self.mode,
                        vllm_key=self.vllm_key, max_tokens=OPS_MAX_TOKENS,
                    )

                try:
                    result = await self.scheduler.submit(
                        BACKGROUND, "compactar", call, est_tokens=est(prompt, OPS_MAX_TOKENS)
                    )
                except Exception:  # noqa: BLE001
                    continue
                if result is None:
                    continue
                max_ts = max((s.end for s in self.state.segments), default=None)
                new = validate_compaction(snapshot, topic.pontos, result, max_ts)
                if new is None:
                    log.info("Compactação do tópico %s rejeitada (perderia conteúdo)", topic.id)
                    continue
                if len(new) < len(topic.pontos):
                    topic.pontos = new
                    changed += 1
            if changed:
                self.save([{"op": "compactar", "topicos": changed}])
        return changed

    async def close(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
