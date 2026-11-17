"""Histórico das dailies: SQLite + arquivos por data em data/YYYY-MM-DD/."""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import asdict
from pathlib import Path

from wendes.config import Config
from wendes.facilitator.state_machine import MeetingRecord

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS meetings (
    date TEXT,
    started_at TEXT,
    record_json TEXT,
    ata_markdown TEXT,
    PRIMARY KEY (date, started_at)
);
"""


class HistoryStore:
    def __init__(self, cfg: Config) -> None:
        self.data_dir = cfg.resolve(cfg.data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.data_dir / "history.sqlite")
        self._migrate_legacy_schema()
        self.db.executescript(SCHEMA)

    def _migrate_legacy_schema(self) -> None:
        row = self.db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='meetings'"
        ).fetchone()
        if row and "PRIMARY KEY (date, started_at)" not in row[0]:
            self.db.execute("ALTER TABLE meetings RENAME TO meetings_legacy")
            self.db.commit()
            log.info("Schema antigo migrado para meetings_legacy")

    def run_dir(self, date: str, started_at: str) -> Path:
        """Uma pasta por execução: data/YYYY-MM-DD/HH-MM/."""
        d = self.data_dir / date / started_at.replace(":", "-")
        d.mkdir(parents=True, exist_ok=True)
        return d

    def save(self, record: MeetingRecord, ata_markdown: str) -> Path:
        record_json = json.dumps(asdict(record), ensure_ascii=False, indent=1)
        self.db.execute(
            "INSERT OR REPLACE INTO meetings (date, started_at, record_json, ata_markdown)"
            " VALUES (?, ?, ?, ?)",
            (record.date, record.started_at, record_json, ata_markdown),
        )
        self.db.commit()
        run = self.run_dir(record.date, record.started_at)
        (run / "record.json").write_text(record_json, encoding="utf-8")
        ata_path = run / "ata.md"
        ata_path.write_text(ata_markdown, encoding="utf-8")
        log.info("Histórico salvo em %s", run)
        return ata_path

    def list_runs(self, date: str) -> list[str]:
        rows = self.db.execute(
            "SELECT started_at FROM meetings WHERE date = ? ORDER BY started_at", (date,)
        ).fetchall()
        return [r[0] for r in rows]

    def load_record(self, date: str, started_at: str | None = None) -> MeetingRecord | None:
        """Carrega uma reunião; sem `started_at`, a última do dia."""
        if started_at is None:
            row = self.db.execute(
                "SELECT record_json FROM meetings WHERE date = ?"
                " ORDER BY started_at DESC LIMIT 1",
                (date,),
            ).fetchone()
        else:
            row = self.db.execute(
                "SELECT record_json FROM meetings WHERE date = ? AND started_at = ?",
                (date, started_at),
            ).fetchone()
        if not row:
            return None
        from wendes.facilitator.state_machine import Answer, PersonRecord

        data = json.loads(row[0])
        people = [
            PersonRecord(
                name=p["name"],
                present=p["present"],
                has_blocker=p["has_blocker"],
                answers=[Answer(**a) for a in p["answers"]],
            )
            for p in data["people"]
        ]
        return MeetingRecord(
            date=data["date"],
            started_at=data["started_at"],
            people=people,
            raw_utterances=data.get("raw_utterances", []),
            interrupted=data.get("interrupted", False),
            error=data.get("error", ""),
            mode=data.get("mode", ""),
        )
