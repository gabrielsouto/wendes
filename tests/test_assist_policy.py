from __future__ import annotations

import os
from datetime import date

from conftest import FakeRoom, make_cfg
from wendes.assistant.policy import Policy, purge
from wendes.facilitator.auth import AdminAuth


def _policy(**perms):
    cfg = make_cfg(assistant={"permissions": perms} if perms else {})
    return Policy(cfg, AdminAuth(cfg, FakeRoom())), cfg


def test_matriz_de_permissoes():
    pol, _ = _policy()
    pol.auth.grant("adm")
    assert pol.can_ask("p", "Fulana da Silva")  # members por padrão
    assert pol.can_ask("p", "fsilva")  # alias do roster
    assert not pol.can_ask("p", "Fulana Hacker")
    assert pol.can_ask("adm", "Qualquer Nome")
    assert pol.can_artifacts("p", "Beltrano")
    assert not pol.can_history("p", "Beltrano", via_voice=False, unknown_present=False)
    assert pol.can_history("adm", "", via_voice=False, unknown_present=False)
    assert not pol.can_history("adm", "", via_voice=True, unknown_present=False)
    assert not pol.can_history("adm", "", via_voice=False, unknown_present=True)

    pol, _ = _policy(ask="all", history="members", artifacts="admins")
    assert pol.can_ask("x", "Visitante")
    assert pol.can_history("p", "Beltrano", via_voice=False, unknown_present=False)
    assert not pol.can_artifacts("p", "Beltrano")
    pol, _ = _policy(history="none")
    pol.auth.grant("adm")
    assert not pol.can_history("adm", "", via_voice=False, unknown_present=False)


def test_desconhecido():
    pol, _ = _policy()
    assert pol.is_unknown("x", "Convidado")
    assert not pol.is_unknown("x", "Wendes")
    assert not pol.is_unknown("x", "fulana da silva")


def test_purge_respeita_dias_e_nunca_apaga_ata(tmp_path):
    cfg = make_cfg(
        data_dir=str(tmp_path / "data"),
        data_retention={"recording_days": 30, "transcript_days": 60},
    )
    data = tmp_path / "data"
    files = {}
    for day in ("2026-01-01", "2026-09-30", "2026-11-10"):
        run = data / day / "09-30"
        run.mkdir(parents=True)
        for name in ("recording.wav", "transcript.json", "live_transcript.jsonl", "ata.md",
                     "notes.json"):
            (run / name).write_text("x")
        (run / "page").mkdir()
        (run / "page" / "index.html").write_text("x")
        files[day] = run
    (data / "history.sqlite").write_text("")
    today = date(2026, 11, 17)

    dry = purge(cfg, dry_run=True, today=today)
    assert all(p.exists() for p in dry)
    removed = purge(cfg, today=today)
    assert set(dry) == set(removed)
    old, mid, new = files["2026-01-01"], files["2026-09-30"], files["2026-11-10"]
    assert not (old / "recording.wav").exists() and not (old / "transcript.json").exists()
    assert not (old / "page").exists()
    assert not (mid / "recording.wav").exists() and (mid / "transcript.json").exists()
    assert (new / "recording.wav").exists()
    for run in (old, mid, new):
        assert (run / "ata.md").exists() and (run / "notes.json").exists()


def test_purge_zero_mantem_tudo(tmp_path):
    cfg = make_cfg(
        data_dir=str(tmp_path / "data"),
        data_retention={"recording_days": 0, "transcript_days": 0},
    )
    run = tmp_path / "data" / "2020-01-01" / "09-30"
    run.mkdir(parents=True)
    (run / "recording.wav").write_text("x")
    assert purge(cfg, today=date(2026, 1, 1)) == []
    assert os.path.exists(run / "recording.wav")
