"""Estado da reunião no modo assist (fonte única de verdade) e barramento de eventos.

Tudo que muda no `MeetingState` vira um evento no `EventBus`, consumido pela
página (SSE) e por quem mais se inscrever. O estado é persistido em arquivos
JSONL/JSON na pasta da execução e pode ser reconstruído com `MeetingState.load`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

GAP_TEXT = "[trecho não transcrito ao vivo]"


def mmss(t: float) -> str:
    t = max(0, int(t))
    h, rest = divmod(t, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


@dataclass
class Segment:
    id: int
    start: float
    end: float
    text: str
    speaker: str = ""  # "" = não identificado
    speaker_conf: float = 0.0
    source: str = "live"  # live | batch | bot
    marker: bool = False  # lacuna, pausa: não é fala

    def line(self, unknown: str = "?") -> str:
        """Formato dos prompts: `[mm:ss] Nome: texto`."""
        who = self.speaker or unknown
        return f"[{mmss(self.start)}] {who}: {self.text}"


class EventBus:
    """Pub/sub em asyncio: cada inscrito tem uma fila limitada (descarta o mais antigo)."""

    def __init__(self) -> None:
        self._subs: list[asyncio.Queue] = []

    def subscribe(self, maxsize: int = 1000) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._subs.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._subs:
            self._subs.remove(q)

    @property
    def subscribers(self) -> int:
        return len(self._subs)

    def publish(self, kind: str, data: Any) -> None:
        for q in self._subs:
            try:
                q.put_nowait((kind, data))
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:  # pragma: no cover
                    pass
                q.put_nowait((kind, data))


def append_jsonl(path: Path, row: dict) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # última linha cortada se o processo morreu
    return out


class MeetingState:
    def __init__(
        self,
        run_dir: Path | None,
        *,
        run_id: str = "",
        date: str = "",
        started_at: str = "",
        room_url: str = "",
        bot_name: str = "Wendes",
        persist: bool = True,
    ) -> None:
        self.run_dir = run_dir
        self.run_id = run_id
        self.date = date
        self.started_at = started_at
        self.room_url = room_url
        self.bot_name = bot_name
        self.persist = persist and run_dir is not None
        self.bus = EventBus()
        self.segments: list[Segment] = []
        self.participants: dict[str, str] = {}  # pid -> display name
        self.status = "recording"  # recording | paused | finished
        self.paused_by = ""
        self.stt_lag = 0.0
        self.llm_queue = 0
        self.position = 0.0
        self.notes: dict = {}
        self.prev_actions: list[dict] = []
        self.qa: list[dict] = []
        self.artifacts: list[dict] = []
        self.wall_start = time.time()
        self._next_id = 1

    # ---- arquivos --------------------------------------------------------
    def path(self, name: str) -> Path:
        assert self.run_dir is not None
        return self.run_dir / name

    # ---- transcrição -----------------------------------------------------
    def add_segment(
        self,
        start: float,
        end: float,
        text: str,
        *,
        speaker: str = "",
        speaker_conf: float = 0.0,
        source: str = "live",
        marker: bool = False,
    ) -> Segment:
        seg = Segment(
            self._next_id, round(start, 2), round(end, 2), text.strip(),
            speaker, round(speaker_conf, 3), source, marker,
        )
        self._next_id += 1
        self.segments.append(seg)
        if self.persist:
            append_jsonl(self.path("live_transcript.jsonl"), asdict(seg))
        self.bus.publish("segment", asdict(seg))
        return seg

    def text_since(self, seg_id: int) -> list[Segment]:
        return [s for s in self.segments if s.id > seg_id and not s.marker]

    # ---- status ----------------------------------------------------------
    def set_status(self, status: str, paused_by: str = "") -> None:
        self.status = status
        self.paused_by = paused_by
        self.publish_status()

    def publish_status(self) -> None:
        self.bus.publish("status", self.status_dict())

    def status_dict(self) -> dict:
        return {
            "status": self.status,
            "paused_by": self.paused_by,
            "stt_lag": round(self.stt_lag, 1),
            "llm_queue": self.llm_queue,
            "position": round(self.position, 1),
            "participants": sorted(set(self.participants.values())),
        }

    def set_participants(self, participants: dict[str, str]) -> None:
        if participants != self.participants:
            self.participants = dict(participants)
            self.publish_status()

    # ---- partes que outras fases preenchem ------------------------------
    def set_notes(self, notes: dict) -> None:
        self.notes = notes
        self.bus.publish("notes", notes)

    def set_prev_actions(self, items: list[dict]) -> None:
        self.prev_actions = items
        self.bus.publish("prev_actions", items)

    def add_qa(self, item: dict) -> None:
        self.qa.append(item)
        if self.persist:
            append_jsonl(self.path("qa.jsonl"), item)
        self.bus.publish("qa", item)

    def set_artifacts(self, items: list[dict]) -> None:
        self.artifacts = items
        self.bus.publish("artifacts", items)

    # ---- snapshot --------------------------------------------------------
    def snapshot(self) -> dict:
        return {
            "run_id": self.run_id,
            "date": self.date,
            "started_at": self.started_at,
            "room_url": self.room_url,
            "bot_name": self.bot_name,
            **self.status_dict(),
            "segments": [asdict(s) for s in self.segments],
            "notes": self.notes,
            "prev_actions": self.prev_actions,
            "qa": self.qa,
            "artifacts": self.artifacts,
        }

    @classmethod
    def load(cls, run_dir: Path, **kwargs: Any) -> MeetingState:
        """Reconstrói o estado a partir dos arquivos da execução (sem regravar nada)."""
        st = cls(run_dir, persist=False, **kwargs)
        for row in read_jsonl(run_dir / "live_transcript.jsonl"):
            fields = {k: row[k] for k in Segment.__dataclass_fields__ if k in row}
            try:
                st.segments.append(Segment(**fields))
            except TypeError:
                continue
        if st.segments:
            st._next_id = max(s.id for s in st.segments) + 1
        notes = run_dir / "notes.json"
        if notes.exists():
            try:
                st.notes = json.loads(notes.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                log.warning("notes.json ilegível em %s", run_dir)
        st.qa = read_jsonl(run_dir / "qa.jsonl")
        index = run_dir / "artifacts" / "artifacts.json"
        if index.exists():
            try:
                st.artifacts = json.loads(index.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        prev = run_dir / "prev_actions.json"
        if prev.exists():
            try:
                st.prev_actions = json.loads(prev.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        st.status = "finished"
        return st
