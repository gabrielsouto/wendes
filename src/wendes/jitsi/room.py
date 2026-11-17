"""Interação com a conferência via API interna APP.conference da página Jitsi."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from playwright.async_api import Page

log = logging.getLogger(__name__)

JOINED_JS = (
    "() => !!(window.APP && APP.conference && APP.conference._room"
    " && APP.conference._room.isJoined && APP.conference._room.isJoined())"
)


@dataclass
class Participant:
    id: str
    display_name: str


class JitsiRoom:
    def __init__(self, page: Page) -> None:
        self.page = page
        self.dominant_speaker_id: str | None = None
        self._speaker_listeners: list = []
        self._chat_listeners: list = []

    async def wait_joined(self, timeout_s: int = 90) -> None:
        """Espera a conferência estar estabelecida (APP.conference.membersCount)."""
        deadline = asyncio.get_event_loop().time() + timeout_s
        while asyncio.get_event_loop().time() < deadline:
            joined = await self.page.evaluate(JOINED_JS)
            if joined:
                log.info("Entrou na conferência")
                return
            await asyncio.sleep(1)
        raise TimeoutError("Não entrou na conferência dentro do tempo limite")

    async def install_event_bridge(self) -> None:
        """Escuta dominant-speaker e mudanças de participantes via expose_function."""

        async def _on_dominant_speaker(pid: str, display_name: str = "") -> None:
            self.dominant_speaker_id = pid
            for cb in self._speaker_listeners:
                try:
                    cb(pid, display_name)
                except TypeError:
                    cb(pid)

        async def _on_chat_message(pid: str, text: str) -> None:
            log.info("Chat recebido de %s: %s", pid, text[:120])
            for cb in self._chat_listeners:
                res = cb(pid, text)
                if asyncio.iscoroutine(res):
                    await res

        await self.page.expose_function("botOnDominantSpeaker", _on_dominant_speaker)
        await self.page.expose_function("botOnChatMessage", _on_chat_message)
        diag = await self.page.evaluate(
            """() => {
                const out = { jitsiMeetJS: !!window.JitsiMeetJS, listeners: [], errors: [] };
                try {
                    const room = APP.conference._room;
                    const events = (window.JitsiMeetJS && JitsiMeetJS.events.conference) || {};
                    const myId = String(APP.conference.getMyUserId());
                    const forward = (label) => (id, text) => {
                        if (String(id) !== myId && typeof text === 'string') {
                            window.botOnChatMessage(String(id), String(text));
                        }
                    };
                    const speakerName = (id) => {
                        try {
                            const participant = room.getParticipantById(id);
                            return participant && participant.getDisplayName
                                ? participant.getDisplayName() || ''
                                : '';
                        } catch (e) {
                            return '';
                        }
                    };
                    const listen = (ev, fallback, label) => {
                        const name = ev || fallback;
                        room.on(name, label === 'speaker'
                            ? (id) => window.botOnDominantSpeaker(String(id), speakerName(id))
                            : forward(label));
                        out.listeners.push(name);
                    };
                    listen(events.DOMINANT_SPEAKER_CHANGED,
                           'conference.dominantSpeaker', 'speaker');
                    listen(events.MESSAGE_RECEIVED, 'conference.messageReceived', 'chat');
                    listen(events.PRIVATE_MESSAGE_RECEIVED,
                           'conference.privateMessageReceived', 'private');
                } catch (e) {
                    out.errors.push(String(e));
                }
                return out;
            }"""
        )
        log.info("Ponte de eventos instalada: %s", diag)

    def on_dominant_speaker(self, callback) -> None:
        self._speaker_listeners.append(callback)

    def on_chat_message(self, callback) -> None:
        """callback(participant_id: str, text: str) — pode ser corrotina."""
        self._chat_listeners.append(callback)

    async def get_display_name(self, participant_id: str) -> str:
        for p in await self.list_participants():
            if p.id == participant_id:
                return p.display_name
        return ""

    async def list_participants(self) -> list[Participant]:
        raw = await self.page.evaluate(
            """() => APP.conference.listMembers().map(m => ({
                id: m.getId(), name: m.getDisplayName() || ''
            }))"""
        )
        return [Participant(id=r["id"], display_name=r["name"]) for r in raw]

    async def unmute(self) -> None:
        muted = await self.page.evaluate("() => APP.conference.isLocalAudioMuted()")
        if muted:
            await self.page.evaluate("() => APP.conference.toggleAudioMuted()")
            log.info("Microfone desmutado")

    async def mute(self) -> None:
        muted = await self.page.evaluate("() => APP.conference.isLocalAudioMuted()")
        if not muted:
            await self.page.evaluate("() => APP.conference.toggleAudioMuted()")

    async def send_chat(self, message: str) -> None:
        await self.page.evaluate(
            "(msg) => APP.conference._room.sendTextMessage(msg)", message
        )

    async def hangup(self) -> None:
        try:
            await self.page.evaluate("() => APP.conference.hangup()")
        except Exception:
            pass
