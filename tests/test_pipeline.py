from __future__ import annotations

import json
import wave

import pytest

import wendes.__main__ as cli
import wendes.distribution.email as email_mod
import wendes.distribution.webhook as webhook_mod
import wendes.minutes.summarizer as summarizer
import wendes.pipeline as pipeline
from wendes.facilitator.state_machine import Answer, MeetingRecord, PersonRecord
from wendes.history.store import HistoryStore


@pytest.fixture
def no_llm(monkeypatch):
    monkeypatch.setattr(pipeline, "_make_llm", lambda cfg: None)


def _interrupted_record() -> MeetingRecord:
    return MeetingRecord(
        date="2026-07-03",
        started_at="09:30",
        people=[PersonRecord(name="Fulana da Silva", present=True)],
        interrupted=True,
        error="Target page, context or browser has been closed",
    )


@pytest.mark.parametrize("mode", ["run", "stealth"])
def test_finalize_sem_wav_e_sem_llm_gera_ata_com_aviso(cfg, no_llm, mode):
    store = HistoryStore(cfg)
    rec = _interrupted_record()
    run_dir = store.run_dir(rec.date, rec.started_at)

    ata_path = pipeline.finalize_meeting(
        cfg, store, run_dir, rec, run_dir / "recording.wav", mode=mode
    )

    ata = ata_path.read_text(encoding="utf-8")
    assert "⚠ Reunião interrompida: Target page, context or browser has been closed." in ata
    assert "A ata cobre só o que foi gravado." in ata
    assert "Participaram: Fulana da Silva." in ata  # resumo de fallback
    saved = store.load_record(rec.date, rec.started_at)
    assert saved.interrupted is True
    assert saved.mode == mode


def test_ata_sem_interrupcao_nao_mostra_aviso(cfg, no_llm):
    store = HistoryStore(cfg)
    rec = MeetingRecord(date="2026-07-03", started_at="09:30")
    run_dir = store.run_dir(rec.date, rec.started_at)
    ata = pipeline.finalize_meeting(
        cfg, store, run_dir, rec, run_dir / "recording.wav", mode="run"
    ).read_text(encoding="utf-8")
    assert "interrompida" not in ata
    assert ata.startswith("# Ata da Daily — 2026-07-03\n\n**Início:** 09:30")


def test_falha_no_smtp_nao_impede_o_webhook(cfg, no_llm, monkeypatch, caplog):
    posted: list[str] = []

    def smtp_quebrado(cfg, ata, date):
        raise OSError("SMTP fora do ar")

    monkeypatch.setattr(email_mod, "send_ata", smtp_quebrado)
    monkeypatch.setattr(webhook_mod, "post_ata", lambda cfg, ata: posted.append(ata))

    store = HistoryStore(cfg)
    rec = _interrupted_record()
    run_dir = store.run_dir(rec.date, rec.started_at)
    pipeline.finalize_meeting(cfg, store, run_dir, rec, run_dir / "x.wav", mode="run")

    assert len(posted) == 1
    assert "Falha ao enviar a ata por e-mail" in caplog.text


def test_falha_no_webhook_nao_propaga(cfg, no_llm, monkeypatch, caplog):
    def webhook_quebrado(cfg, ata):
        raise RuntimeError("500")

    monkeypatch.setattr(webhook_mod, "post_ata", webhook_quebrado)
    store = HistoryStore(cfg)
    rec = _interrupted_record()
    run_dir = store.run_dir(rec.date, rec.started_at)
    pipeline.finalize_meeting(cfg, store, run_dir, rec, run_dir / "x.wav", mode="run")
    assert "Falha ao publicar a ata no webhook" in caplog.text


def test_finalize_so_retranscreve_com_mais_de_1s(cfg, no_llm, monkeypatch, tmp_path):
    import wendes.speech.stt_batch as stt_batch

    calls: list = []
    monkeypatch.setattr(stt_batch, "transcribe_recording", lambda c, p: calls.append(p) or [])

    def wav(path, seconds):
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16_000)
            w.writeframes(b"\x00\x00" * int(16_000 * seconds))
        return path

    store = HistoryStore(cfg)
    rec = MeetingRecord(date="2026-07-03", started_at="09:30")
    run_dir = store.run_dir(rec.date, rec.started_at)
    pipeline.finalize_meeting(cfg, store, run_dir, rec, wav(tmp_path / "a.wav", 0.5), mode="run")
    assert calls == []
    pipeline.finalize_meeting(cfg, store, run_dir, rec, wav(tmp_path / "b.wav", 2), mode="run")
    assert calls == [tmp_path / "b.wav"]


def test_wav_seconds_estima_pelo_tamanho_com_cabecalho_aberto(tmp_path):
    path = tmp_path / "quebrado.wav"
    path.write_bytes(b"RIFF" + b"\x00" * 40 + b"\x00" * 64_000)  # cabeçalho inválido
    assert pipeline.wav_seconds(path) == pytest.approx(2.0)
    assert pipeline.wav_seconds(tmp_path / "nao_existe.wav") == 0.0


def test_load_record_antigo_sem_campos_novos(cfg):
    store = HistoryStore(cfg)
    legacy = {
        "date": "2026-07-01",
        "started_at": "09:30",
        "people": [
            {
                "name": "Fulana da Silva",
                "present": True,
                "has_blocker": False,
                "answers": [{"question": "q", "text": "t"}],
            }
        ],
        "raw_utterances": [],
    }
    store.db.execute(
        "INSERT INTO meetings (date, started_at, record_json, ata_markdown) VALUES (?,?,?,?)",
        ("2026-07-01", "09:30", json.dumps(legacy), "ata"),
    )
    store.db.commit()
    rec = store.load_record("2026-07-01")
    assert rec.interrupted is False
    assert rec.error == ""
    assert rec.mode == ""
    assert rec.people[0].answers == [Answer(question="q", text="t")]


@pytest.mark.parametrize(
    ("mode", "answers", "expected"),
    [
        ("stealth", True, "stealth"),  # mode manda, mesmo com respostas
        ("run", False, "run"),  # mode manda, mesmo sem respostas
        ("", False, "stealth"),  # registro antigo: inferência
        ("", True, "run"),
    ],
)
def test_resummarize_escolhe_gerador_por_mode(cfg, monkeypatch, mode, answers, expected):
    store = HistoryStore(cfg)
    rec = MeetingRecord(date="2026-07-03", started_at="09:30", mode=mode)
    if answers:
        rec.people.append(
            PersonRecord(name="Fulana", present=True, answers=[Answer("q", "t")])
        )
    store.save(rec, "")
    store.db.close()

    used: list[str] = []
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    monkeypatch.setattr(
        summarizer, "generate_ata", lambda *a, **k: used.append("run") or "# run"
    )
    monkeypatch.setattr(
        summarizer, "generate_stealth_ata", lambda *a, **k: used.append("stealth") or "# st"
    )
    assert cli.cmd_resummarize("2026-07-03", None, email=False) == 0
    assert used == [expected]
