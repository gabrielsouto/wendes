from __future__ import annotations

import json
import sqlite3

from wendes.facilitator.state_machine import Answer, MeetingRecord, PersonRecord
from wendes.history.store import HistoryStore


def _record() -> MeetingRecord:
    return MeetingRecord(
        date="2026-07-03",
        started_at="09:30",
        people=[
            PersonRecord(
                name="Fulana da Silva",
                present=True,
                has_blocker=True,
                answers=[Answer(question="O que você fez ontem?", text="Deploy")],
            ),
            PersonRecord(name="Beltrano Souza", present=False),
        ],
        raw_utterances=[{"speaker": "Fulana da Silva", "question": "q", "text": "Deploy"}],
    )


def test_save_load_ida_e_volta(cfg):
    store = HistoryStore(cfg)
    rec = _record()
    ata_path = store.save(rec, "# Ata")
    assert ata_path.read_text(encoding="utf-8") == "# Ata"
    assert (ata_path.parent / "record.json").exists()
    assert ata_path.parent.name == "09-30"

    loaded = store.load_record("2026-07-03")
    assert loaded == rec
    assert store.load_record("2026-07-03", "09:30") == rec
    assert store.load_record("2026-07-04") is None
    assert store.list_runs("2026-07-03") == ["09:30"]


def test_load_sem_hora_pega_a_ultima_do_dia(cfg):
    store = HistoryStore(cfg)
    first = _record()
    second = _record()
    second.started_at = "14:00"
    store.save(first, "a")
    store.save(second, "b")
    assert store.load_record("2026-07-03").started_at == "14:00"
    assert store.list_runs("2026-07-03") == ["09:30", "14:00"]


def test_migra_schema_legado(cfg):
    data_dir = cfg.resolve(cfg.data_dir)
    data_dir.mkdir(parents=True)
    db = sqlite3.connect(data_dir / "history.sqlite")
    db.execute(
        "CREATE TABLE meetings (date TEXT PRIMARY KEY, record_json TEXT, ata_markdown TEXT)"
    )
    db.execute(
        "INSERT INTO meetings VALUES (?, ?, ?)",
        ("2026-01-01", json.dumps({"date": "2026-01-01"}), "velha"),
    )
    db.commit()
    db.close()

    store = HistoryStore(cfg)
    tables = {
        r[0] for r in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"meetings", "meetings_legacy"} <= tables
    legacy = store.db.execute("SELECT ata_markdown FROM meetings_legacy").fetchall()
    assert legacy == [("velha",)]
    # A tabela nova funciona normalmente depois da migração
    store.save(_record(), "# Ata")
    assert store.load_record("2026-07-03") == _record()
    # Reabrir não migra de novo
    HistoryStore(cfg)
