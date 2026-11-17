from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

import wendes.jitsi.browser as browser_mod
from conftest import make_cfg
from wendes.speech.stt_batch import resolve_stt_device, stt_cpu_threads


def test_executavel_vazio_usa_chromium_do_sistema_se_existir(monkeypatch):
    monkeypatch.setattr(Path, "exists", lambda self: str(self) == browser_mod.CHROMIUM_PATH)
    assert browser_mod.resolve_executable(make_cfg()) == browser_mod.CHROMIUM_PATH


def test_executavel_vazio_sem_chromium_do_sistema_usa_o_do_playwright(monkeypatch):
    monkeypatch.setattr(Path, "exists", lambda self: False)
    assert browser_mod.resolve_executable(make_cfg()) is None


def test_executavel_configurado(monkeypatch):
    monkeypatch.setattr(Path, "exists", lambda self: str(self) == "/opt/chrome/chrome")
    cfg = make_cfg(browser={"executable_path": "/opt/chrome/chrome"})
    assert browser_mod.resolve_executable(cfg) == "/opt/chrome/chrome"


def test_executavel_configurado_inexistente_falha_com_mensagem_clara(monkeypatch):
    monkeypatch.setattr(Path, "exists", lambda self: "chromium-browser" in str(self))
    cfg = make_cfg(browser={"executable_path": "/nao/existe/chrome"})
    with pytest.raises(FileNotFoundError, match="browser.executable_path"):
        browser_mod.resolve_executable(cfg)


def _fake_ct2(monkeypatch, count: int) -> None:
    mod = types.ModuleType("ctranslate2")
    mod.get_cuda_device_count = lambda: count
    monkeypatch.setitem(sys.modules, "ctranslate2", mod)


@pytest.mark.parametrize(("count", "expected"), [(0, "cpu"), (2, "cuda")])
def test_dispositivo_auto(monkeypatch, count, expected):
    _fake_ct2(monkeypatch, count)
    assert resolve_stt_device(make_cfg(stt={"device": "auto"})) == expected


def test_dispositivo_explicito_nao_consulta_gpu(monkeypatch):
    _fake_ct2(monkeypatch, 4)
    assert resolve_stt_device(make_cfg(stt={"device": "cpu"})) == "cpu"
    assert resolve_stt_device(make_cfg()) == "cpu"  # padrão
    assert resolve_stt_device(make_cfg(stt={"device": "cuda"})) == "cuda"


def test_cpu_threads(monkeypatch):
    monkeypatch.setattr("os.cpu_count", lambda: 12)
    assert stt_cpu_threads(make_cfg()) == 12
    assert stt_cpu_threads(make_cfg(stt={"cpu_threads": 3})) == 3
