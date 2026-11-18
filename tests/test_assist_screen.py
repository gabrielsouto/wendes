"""Fase 7 (F10, experimental): /tela com fakes do Playwright."""
from __future__ import annotations

from conftest import FakeRoom, FakeTranscribe, energy_vad, make_cfg
from wendes.assistant.app import build_session
from wendes.assistant.state import MeetingState
from wendes.audio.capture import AudioCapture
from wendes.jitsi.screen import SCREEN_TITLE, ScreenShare, screen_share_args


class FakeTab:
    def __init__(self):
        self.url = None
        self.init = []
        self.closed = False

    async def add_init_script(self, js):
        self.init.append(js)

    async def set_viewport_size(self, size):
        pass

    async def goto(self, url, **kw):
        self.url = url

    async def close(self):
        self.closed = True


class FakeContext:
    def __init__(self):
        self.pages = []

    async def new_page(self):
        tab = FakeTab()
        self.pages.append(tab)
        return tab


class FakeJitsiPage:
    def __init__(self, works=True):
        self.context = FakeContext()
        self.works = works
        self.sharing = False
        self.calls = []

    async def bring_to_front(self):
        pass

    async def evaluate(self, js, arg=None):
        self.calls.append((js, arg))
        if "toggleScreenSharing" in js:
            self.sharing = bool(arg) and self.works
            return None
        return self.sharing


def test_flags_escolhem_a_fonte_pelo_titulo():
    args = screen_share_args()
    assert f"--auto-select-desktop-capture-source={SCREEN_TITLE}" in args
    assert "--enable-usermedia-screen-capturing" in args


async def test_screen_share_abre_aba_e_liga_e_desliga():
    page = FakeJitsiPage()
    sc = ScreenShare(page, settle_s=0, verify_s=1)
    assert await sc.start("http://127.0.0.1:8765/m/r?token=t")
    tab = page.context.pages[0]
    assert tab.url.endswith("token=t") and SCREEN_TITLE in tab.init[0]
    assert sc.active and page.sharing
    await sc.stop()
    assert not sc.active and not page.sharing and tab.closed


async def test_screen_share_sem_confirmacao_desfaz():
    page = FakeJitsiPage(works=False)
    sc = ScreenShare(page, settle_s=0, verify_s=0.6)
    assert not await sc.start("http://x")
    assert not sc.active and page.context.pages[0].closed


async def _session(tmp_path, screen_share, screen):
    cfg = make_cfg(data_dir=str(tmp_path / "data"), commands={"require_code": True},
                   assistant={"speak_greeting": False, "screen_share": screen_share})
    room = FakeRoom({"p1": "Fulana da Silva"})
    st = MeetingState(tmp_path, run_id="r1", date="2026-11-18", started_at="09:30")
    s = build_session(cfg, room, AudioCapture(tmp_path / "r.wav"), tmp_path, st,
                      transcribe=FakeTranscribe(), vad=energy_vad, web=False,
                      vad_in_thread=False, screen=screen)

    class Web:
        def local_url(self):
            return "http://127.0.0.1:8765/m/r1?token=t"

    s.web = Web()
    return s, room


async def test_comando_tela_so_admin_e_so_ligado(tmp_path):
    page = FakeJitsiPage()
    s, room = await _session(tmp_path, False, ScreenShare(page, settle_s=0, verify_s=1))
    await room.emit_chat("p1", "/tela")
    assert "não tem permissão" in room.sent[-1]
    s.auth.grant("p1")
    await room.emit_chat("p1", "/tela")
    assert "desligado" in room.sent[-1]
    assert not page.calls


async def test_comando_tela_liga_e_desliga(tmp_path):
    page = FakeJitsiPage()
    s, room = await _session(tmp_path, True, ScreenShare(page, settle_s=0, verify_s=1))
    s.auth.grant("p1")
    await room.emit_chat("p1", "/tela off")
    assert "Não estou compartilhando" in room.sent[-1]
    await room.emit_chat("p1", "/tela")
    assert room.sent[-1].startswith("Compartilhando a página")
    assert page.context.pages[0].url.endswith("token=t")
    # o link com token nunca vai para o chat
    assert all("token=" not in m for m in room.sent)
    await room.emit_chat("p1", "/tela off")
    assert room.sent[-1] == "Parei de compartilhar a tela."
    assert not page.sharing


async def test_tela_que_falha_sugere_link(tmp_path):
    page = FakeJitsiPage(works=False)
    s, room = await _session(tmp_path, True, ScreenShare(page, settle_s=0, verify_s=0.6))
    s.auth.grant("p1")
    await room.emit_chat("p1", "/tela")
    assert "/link" in room.sent[-1]
