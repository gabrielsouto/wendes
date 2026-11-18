"""Busca no histórico (FTS5) e na reunião corrente.

Tabelas no mesmo `history.sqlite` do `HistoryStore`:
  seg_fts(meeting_key, seq, ts, speaker, text)  — segmentos da transcrição
  ata_fts(meeting_key, text)                    — atas
com tokenizer `unicode61 remove_diacritics 2` (busca sem acento casa com acento).

Consulta: palavras-chave sem stopwords em pt, `MATCH` com OR, ordenação por
bm25 e os top-k trechos com 2 segmentos vizinhos de cada lado.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

TOKENIZER = "unicode61 remove_diacritics 2"
SCHEMA = f"""
CREATE VIRTUAL TABLE IF NOT EXISTS seg_fts USING fts5(
    meeting_key UNINDEXED, seq UNINDEXED, ts UNINDEXED, speaker, text,
    tokenize='{TOKENIZER}'
);
CREATE VIRTUAL TABLE IF NOT EXISTS ata_fts USING fts5(
    meeting_key UNINDEXED, text, tokenize='{TOKENIZER}'
);
"""

STOPWORDS = set(
    """a o as os um uma uns umas de do da dos das em no na nos nas por pelo pela pelos pelas
    para pra pro com sem sob sobre ate até e ou mas que se como quando onde quem qual quais
    quanto quantos quanta quantas porque por que porquê pq ja já nao não sim mais menos muito
    muita muitos muitas ao aos à às este esta estes estas esse essa esses essas isso isto
    aquilo aquele aquela eu tu ele ela nos nós vos vós eles elas me te lhe nos vocês voce você
    seu sua seus suas meu minha meus minhas nosso nossa nossos nossas foi ser era é são sao
    está esta estão estao estava tem têm tinha ter há ha vai vão vao fica ficou ficaram fez
    fizeram disse falou falaram sobre então entao ai aí lá la aqui também tambem só so
    gente agora hoje ontem depois antes alguém alguem algum alguma coisa coisas reunião
    reuniao wendes""".split()
)
_WORD = re.compile(r"[0-9A-Za-zÀ-ÿ]+")


def _fold(text: str) -> str:
    dec = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in dec if not unicodedata.combining(c))


STOP_FOLDED = {_fold(w) for w in STOPWORDS}


def keywords(text: str, limit: int = 12) -> list[str]:
    out: list[str] = []
    for w in _WORD.findall(_fold(text)):
        if (len(w) >= 3 or w.isdigit()) and w not in STOP_FOLDED and w not in out:
            out.append(w)
    return out[:limit]


def match_query(words: list[str]) -> str:
    """`w1 OR w2*`: palavras de 5+ letras viram prefixo (plural, flexão)."""
    terms = [f'"{w}"*' if len(w) >= 5 else f'"{w}"' for w in words]
    return " OR ".join(terms)


def open_db(data_dir: Path) -> sqlite3.Connection:
    data_dir.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(data_dir / "history.sqlite")
    ensure_schema(db)
    return db


def ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)


def meeting_key(date: str, started_at: str) -> str:
    return f"{date} {started_at}"


def index_meeting(
    db: sqlite3.Connection,
    key: str,
    segments: list[tuple[float, str, str]],
    ata: str = "",
) -> int:
    """(Re)indexa uma reunião: segmentos (ts, falante, texto) e a ata."""
    ensure_schema(db)
    db.execute("DELETE FROM seg_fts WHERE meeting_key = ?", (key,))
    db.execute("DELETE FROM ata_fts WHERE meeting_key = ?", (key,))
    db.executemany(
        "INSERT INTO seg_fts (meeting_key, seq, ts, speaker, text) VALUES (?, ?, ?, ?, ?)",
        [(key, i, float(ts), speaker or "", text) for i, (ts, speaker, text) in
         enumerate(s for s in segments if s[2].strip())],
    )
    if ata.strip():
        db.execute("INSERT INTO ata_fts (meeting_key, text) VALUES (?, ?)", (key, ata))
    db.commit()
    return len(segments)


@dataclass
class Hit:
    meeting_key: str
    ts: float
    lines: list[str]
    score: float = 0.0

    @property
    def date(self) -> str:
        return self.meeting_key.split(" ")[0]


def _fmt(ts: float, speaker: str, text: str) -> str:
    from wendes.assistant.state import mmss

    return f"[{mmss(ts)}] {speaker or '?'}: {text}"


def search_history(
    db: sqlite3.Connection,
    question: str,
    k: int = 5,
    *,
    keys: list[str] | None = None,
    exclude_key: str | None = None,
    neighbors: int = 2,
) -> list[Hit]:
    words = keywords(question)
    if not words:
        return []
    ensure_schema(db)
    sql = (
        "SELECT meeting_key, seq, ts, bm25(seg_fts) AS score FROM seg_fts"
        " WHERE seg_fts MATCH ?"
    )
    params: list = [match_query(words)]
    if keys:
        sql += f" AND meeting_key IN ({','.join('?' * len(keys))})"
        params += keys
    if exclude_key:
        sql += " AND meeting_key != ?"
        params.append(exclude_key)
    sql += " ORDER BY score LIMIT ?"
    params.append(k * 3)
    try:
        rows = db.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        log.warning("Consulta FTS inválida", exc_info=True)
        return []
    hits: list[Hit] = []
    used: set[tuple[str, int]] = set()
    for key, seq, ts, score in rows:
        seq = int(seq)
        if any((key, s) in used for s in range(seq - neighbors, seq + neighbors + 1)):
            continue
        ctx = db.execute(
            "SELECT seq, ts, speaker, text FROM seg_fts WHERE meeting_key = ?"
            " AND CAST(seq AS INTEGER) BETWEEN ? AND ? ORDER BY CAST(seq AS INTEGER)",
            (key, seq - neighbors, seq + neighbors),
        ).fetchall()
        for s, *_ in ctx:
            used.add((key, int(s)))
        hits.append(Hit(key, float(ts), [_fmt(t, sp, tx) for _, t, sp, tx in ctx], score))
        if len(hits) >= k:
            break
    return hits


def search_atas(db: sqlite3.Connection, question: str, k: int = 3,
                exclude_key: str | None = None) -> list[tuple[str, str]]:
    """(meeting_key, trecho) das atas mais relevantes."""
    words = keywords(question)
    if not words:
        return []
    ensure_schema(db)
    sql = ("SELECT meeting_key, snippet(ata_fts, 1, '', '', ' … ', 48) FROM ata_fts"
           " WHERE ata_fts MATCH ?")
    params: list = [match_query(words)]
    if exclude_key:
        sql += " AND meeting_key != ?"
        params.append(exclude_key)
    sql += " ORDER BY bm25(ata_fts) LIMIT ?"
    params.append(k)
    try:
        return [(r[0], r[1]) for r in db.execute(sql, params).fetchall()]
    except sqlite3.OperationalError:
        return []


def search_segments(segments, question: str, k: int = 4, neighbors: int = 2,
                    skip_from_id: int | None = None) -> list[list]:
    """Busca simples na reunião corrente (em memória): blocos de segmentos."""
    words = keywords(question)
    if not words:
        return []
    pool = [s for s in segments if not s.marker and (skip_from_id is None or s.id < skip_from_id)]
    scored = []
    for i, s in enumerate(pool):
        folded = _fold(s.text)
        score = sum(1 for w in words if w in folded)
        if score:
            scored.append((score, i))
    scored.sort(key=lambda x: (-x[0], x[1]))
    blocks: list[list] = []
    used: set[int] = set()
    for _, i in scored:
        if i in used:
            continue
        rng = range(max(0, i - neighbors), min(len(pool), i + neighbors + 1))
        used.update(rng)
        blocks.append([pool[j] for j in rng])
        if len(blocks) >= k:
            break
    blocks.sort(key=lambda b: b[0].start)
    return blocks


# ---- indexação a partir dos arquivos -----------------------------------------------
def segments_from_run(run_dir: Path) -> list[tuple[float, str, str]]:
    """Segmentos da reunião: transcript.json (lote) se houver, senão a transcrição ao vivo."""
    path = run_dir / "transcript.json"
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return [(s["start"], s.get("speaker", ""), s["text"]) for s in data]
        except (ValueError, KeyError):
            pass
    from wendes.assistant.state import read_jsonl

    return [
        (r["start"], r.get("speaker", ""), r["text"])
        for r in read_jsonl(run_dir / "live_transcript.jsonl")
        if not r.get("marker") and r.get("source") != "bot"
    ]


def index_run(db: sqlite3.Connection, run_dir: Path, date: str, started_at: str) -> int:
    ata_path = run_dir / "ata.md"
    ata = ata_path.read_text(encoding="utf-8") if ata_path.exists() else ""
    return index_meeting(db, meeting_key(date, started_at), segments_from_run(run_dir), ata)


def reindex_all(data_dir: Path) -> tuple[int, int]:
    """Indexa todas as reuniões do histórico. Retorna (reuniões, segmentos)."""
    db = open_db(data_dir)
    meetings = segs = 0
    try:
        rows = db.execute("SELECT date, started_at FROM meetings").fetchall()
    except sqlite3.OperationalError:
        rows = []
    for date, started_at in rows:
        run_dir = data_dir / date / started_at.replace(":", "-")
        if not run_dir.exists():
            continue
        segs += index_run(db, run_dir, date, started_at)
        meetings += 1
    db.close()
    return meetings, segs


def index_safely(data_dir: Path, run_dir: Path, date: str, started_at: str) -> None:
    """Indexa no fim da reunião (todos os modos); falha só gera log."""
    try:
        db = open_db(data_dir)
        try:
            n = index_run(db, run_dir, date, started_at)
        finally:
            db.close()
        log.info("Reunião indexada para busca (%d segmentos)", n)
    except Exception:  # noqa: BLE001
        log.exception("Falha ao indexar a reunião para busca")
