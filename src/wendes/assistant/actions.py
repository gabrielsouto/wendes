"""Ações estruturadas e pendências entre reuniões (F5).

Tabela `action_items` no `history.sqlite` (com migração de colunas). As ações
das notas são gravadas no fim da reunião; ao entrar, o assist carrega as
ações abertas da mesma sala dentro de `actions.lookback_days` e as apresenta
como pendências P1…Pn, que o `NotesUpdater` pode marcar como resolvidas
citando o trecho de evidência.

O webhook de ações recebe um payload montado APENAS com campos validados
(pydantic), com 3 tentativas e backoff; o payload fica em
`actions_payload.json` e `wendes send-actions` reenvia.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Callable
from datetime import date as Date
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel

from wendes.assistant.notes import Notes, PrevPending

log = logging.getLogger(__name__)

COLUMNS = {
    "meeting_date": "TEXT",
    "meeting_started_at": "TEXT",
    "room_url": "TEXT",
    "descricao": "TEXT",
    "responsavel": "TEXT",
    "prazo": "TEXT",
    "status": "TEXT",
    "ts": "REAL",
    "resolvido_em_date": "TEXT",
    "resolvido_em_started_at": "TEXT",
    "evidencia": "TEXT",
    "live_id": "TEXT",
}
WEEKDAYS = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]
CHAT_MAX_ITEMS = 10


def normalize_room(url: str) -> str:
    """Sala comparável: host + caminho, sem query/fragmento/barra final, minúsculas."""
    if not url:
        return ""
    parts = urlsplit(url.strip())
    return f"{parts.netloc}{parts.path}".rstrip("/").lower()


def migrate(db: sqlite3.Connection) -> None:
    """Cria `action_items` ou acrescenta colunas que faltem (bancos antigos)."""
    db.execute("CREATE TABLE IF NOT EXISTS action_items (id INTEGER PRIMARY KEY AUTOINCREMENT)")
    have = {row[1] for row in db.execute("PRAGMA table_info(action_items)")}
    for col, kind in COLUMNS.items():
        if col not in have:
            db.execute(f"ALTER TABLE action_items ADD COLUMN {col} {kind}")
    db.execute(
        "CREATE INDEX IF NOT EXISTS action_items_room ON action_items (room_url, status,"
        " meeting_date)"
    )
    db.commit()


class ActionStore:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(db_path)
        self.db.row_factory = sqlite3.Row
        migrate(self.db)

    def close(self) -> None:
        self.db.close()

    def open_items(
        self,
        room_url: str,
        *,
        date: str,
        started_at: str,
        lookback_days: int = 14,
    ) -> list[dict]:
        """Ações abertas de reuniões ANTERIORES da mesma sala, na janela de dias."""
        cutoff = (Date.fromisoformat(date) - timedelta(days=lookback_days)).isoformat()
        rows = self.db.execute(
            "SELECT * FROM action_items WHERE status = 'aberta' AND room_url = ?"
            " AND meeting_date >= ?"
            " AND (meeting_date < ? OR (meeting_date = ? AND meeting_started_at < ?))"
            " ORDER BY meeting_date, meeting_started_at, id",
            (normalize_room(room_url), cutoff, date, date, started_at),
        ).fetchall()
        return [dict(r) for r in rows]

    def save_meeting(
        self, *, date: str, started_at: str, room_url: str, notes: Notes,
    ) -> dict[str, int]:
        """Grava as ações da reunião (idempotente: regravar troca as da mesma reunião).

        Retorna live_id (A1…) → id no banco.
        """
        self.db.execute(
            "DELETE FROM action_items WHERE meeting_date = ? AND meeting_started_at = ?",
            (date, started_at),
        )
        ids: dict[str, int] = {}
        for a in notes.acoes:
            cur = self.db.execute(
                "INSERT INTO action_items (meeting_date, meeting_started_at, room_url, descricao,"
                " responsavel, prazo, status, ts, live_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (date, started_at, normalize_room(room_url), a.descricao, a.responsavel,
                 a.prazo, a.status, a.ts, a.id),
            )
            ids[a.id] = int(cur.lastrowid)
        self.db.commit()
        return ids

    def resolve(self, db_id: int, status: str, *, date: str, started_at: str,
                evidencia: str = "") -> None:
        self.db.execute(
            "UPDATE action_items SET status = ?, resolvido_em_date = ?,"
            " resolvido_em_started_at = ?, evidencia = ? WHERE id = ?",
            (status, date, started_at, evidencia[:500], db_id),
        )
        self.db.commit()

    def get(self, db_id: int) -> dict | None:
        row = self.db.execute("SELECT * FROM action_items WHERE id = ?", (db_id,)).fetchone()
        return dict(row) if row else None


def weekday_pt(date: str) -> str:
    try:
        return WEEKDAYS[Date.fromisoformat(date).weekday()]
    except ValueError:
        return date


def to_pending(rows: list[dict]) -> list[PrevPending]:
    return [
        PrevPending(
            id=f"P{i}",
            descricao=r["descricao"] or "",
            responsavel=r.get("responsavel") or "",
            db_id=r["id"],
            meeting_date=r["meeting_date"] or "",
        )
        for i, r in enumerate(rows, 1)
    ]


SITUACAO = {"sem_mencao": "sem menção", "mencionada": "mencionada",
            "concluida": "resolvida", "cancelada": "cancelada"}


def page_items(items: list[PrevPending]) -> list[dict]:
    return [
        {
            "ref": p.id,
            "descricao": p.descricao,
            "responsavel": p.responsavel,
            "meeting_date": p.meeting_date,
            "situacao": SITUACAO.get(p.status, p.status),
            "evidencia_ts": p.evidencia_ts,
        }
        for p in items
    ]


def pending_chat(items: list[PrevPending]) -> str:
    if not items:
        return "Nenhuma pendência aberta de reuniões anteriores."
    lines = [f"Pendências abertas ({len(items)}):"]
    for p in items[:CHAT_MAX_ITEMS]:
        who = f" — {p.responsavel}" if p.responsavel else ""
        mark = {"concluida": " ✔", "cancelada": " ✘", "mencionada": " (mencionada)"}.get(
            p.status, "")
        lines.append(f"{p.id}. {p.descricao}{who} ({weekday_pt(p.meeting_date)},"
                     f" {p.meeting_date}){mark}")
    if len(items) > CHAT_MAX_ITEMS:
        lines.append(f"… e mais {len(items) - CHAT_MAX_ITEMS} na página.")
    return "\n".join(lines)


# ---- payload do webhook ---------------------------------------------------------
class PayloadMeeting(BaseModel):
    data: str
    inicio: str
    sala: str
    equipe: str = ""


class PayloadAction(BaseModel):
    id: int
    descricao: str
    responsavel: str = ""
    prazo: str = ""
    status: str = "aberta"
    ts: float | None = None


class PayloadResolved(BaseModel):
    id: int
    descricao: str
    evidencia: str = ""


class ActionsPayload(BaseModel):
    reuniao: PayloadMeeting
    acoes: list[PayloadAction] = []
    pendencias_resolvidas: list[PayloadResolved] = []


def build_payload(
    *, date: str, started_at: str, room_url: str, team: str, notes: Notes,
    db_ids: dict[str, int], evidence: dict[str, str],
) -> ActionsPayload:
    return ActionsPayload(
        reuniao=PayloadMeeting(data=date, inicio=started_at, sala=room_url, equipe=team),
        acoes=[
            PayloadAction(
                id=db_ids.get(a.id, 0), descricao=a.descricao, responsavel=a.responsavel,
                prazo=a.prazo, status=a.status, ts=a.ts,
            )
            for a in notes.acoes
        ],
        pendencias_resolvidas=[
            PayloadResolved(id=p.db_id or 0, descricao=p.descricao,
                            evidencia=evidence.get(p.id, ""))
            for p in notes.pendencias_anteriores
            if p.status in ("concluida", "cancelada") and p.db_id
        ],
    )


def post_actions(
    url: str,
    payload: ActionsPayload,
    *,
    attempts: int = 3,
    backoff_s: float = 2.0,
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """POST com até `attempts` tentativas (backoff exponencial); True se entregou."""
    body = payload.model_dump()
    for i in range(attempts):
        try:
            with httpx.Client(transport=transport, timeout=30) as client:
                resp = client.post(url, json=body)
            if resp.status_code < 400:
                log.info("Ações publicadas no webhook (%d ações)", len(payload.acoes))
                return True
            if 400 <= resp.status_code < 500 and resp.status_code != 429:
                log.error("Webhook de ações recusou o payload (HTTP %d)", resp.status_code)
                return False
            log.warning("Webhook de ações respondeu %d (tentativa %d/%d)",
                        resp.status_code, i + 1, attempts)
        except httpx.HTTPError as exc:
            log.warning("Webhook de ações falhou (%s, tentativa %d/%d)",
                        type(exc).__name__, i + 1, attempts)
        if i + 1 < attempts:
            sleep(backoff_s * 2**i)
    return False


def save_payload(run_dir: Path, payload: ActionsPayload) -> Path:
    path = run_dir / "actions_payload.json"
    path.write_text(json.dumps(payload.model_dump(), ensure_ascii=False, indent=1),
                    encoding="utf-8")
    return path


def load_payload(run_dir: Path) -> ActionsPayload | None:
    path = run_dir / "actions_payload.json"
    if not path.exists():
        return None
    return ActionsPayload.model_validate_json(path.read_text(encoding="utf-8"))
