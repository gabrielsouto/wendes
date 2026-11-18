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


def _kind(is_private: bool) -> str:
    return "privada" if is_private else "pública"


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
        self._level_listeners: list = []
        self._participant_listeners: list = []
        self.can_send_private: bool | None = None  # descoberto na ponte de eventos
        # Modo assist: o texto do chat só vai ao log em DEBUG (INFO = tamanho e id)
        self.log_text = True

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

        async def _on_chat_message(pid: str, text: str, is_private: bool = False) -> None:
            if text.strip().lower().startswith("/auth"):
                # não registra o código no log
                log.info("Chat recebido de %s (%s): /auth ***", pid, _kind(is_private))
            elif self.log_text:
                log.info("Chat recebido de %s (%s): %s", pid, _kind(is_private), text[:120])
            else:
                log.info(
                    "Chat recebido de %s (%s): %d caracteres", pid, _kind(is_private), len(text)
                )
                log.debug("Chat de %s: %s", pid, text[:500])
            for cb in self._chat_listeners:
                res = cb(pid, text, bool(is_private))
                if asyncio.iscoroutine(res):
                    await res

        await self.page.expose_function("botOnDominantSpeaker", _on_dominant_speaker)
        await self.page.expose_function("botOnChatMessage", _on_chat_message)
        diag = await self.page.evaluate(
            """() => {
                const out = { jitsiMeetJS: !!window.JitsiMeetJS, listeners: [], errors: [] };
                try {
                    const room = APP.conference._room;
                    out.sendPrivateTextMessage = typeof room.sendPrivateTextMessage === 'function';
                    const events = (window.JitsiMeetJS && JitsiMeetJS.events.conference) || {};
                    const myId = String(APP.conference.getMyUserId());
                    const forward = (label) => (id, text) => {
                        if (String(id) !== myId && typeof text === 'string') {
                            window.botOnChatMessage(
                                String(id), String(text), label === 'private');
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
        self.can_send_private = bool(diag.get("sendPrivateTextMessage"))
        if not self.can_send_private:
            log.warning(
                "sendPrivateTextMessage indisponível nesta versão do lib-jitsi-meet —"
                " respostas privadas irão ao chat público (sem conteúdo sensível)"
            )

    async def install_assist_bridge(self, levels: bool = True) -> dict:
        """Ponte extra do modo assist: níveis de áudio (em lotes) e entradas/saídas.

        Os níveis chegam várias vezes por segundo por participante: o JS acumula
        e entrega um lote a cada 250 ms, para não fazer uma chamada ao Python
        por evento. Retorna o diagnóstico (inclui os nomes de eventos
        disponíveis em JitsiMeetJS.events.conference, úteis para a Fase 0).
        """

        async def _on_audio_levels(batch: list) -> None:
            items = [(str(pid), lvl) for pid, lvl in batch]
            for cb in self._level_listeners:
                cb(items)

        async def _on_participant(kind: str, pid: str, name: str = "") -> None:
            for cb in self._participant_listeners:
                res = cb(kind, pid, name)
                if asyncio.iscoroutine(res):
                    await res

        await self.page.expose_function("botOnAudioLevels", _on_audio_levels)
        await self.page.expose_function("botOnParticipant", _on_participant)
        diag = await self.page.evaluate(
            """(withLevels) => {
                const out = { listeners: [], errors: [], eventNames: [] };
                try {
                    const room = APP.conference._room;
                    const events = (window.JitsiMeetJS && JitsiMeetJS.events.conference) || {};
                    out.eventNames = Object.keys(events);
                    const myId = String(APP.conference.getMyUserId());
                    const name = (user) => {
                        try { return (user && user.getDisplayName && user.getDisplayName()) || ''; }
                        catch (e) { return ''; }
                    };
                    const on = (ev, fallback, fn) => {
                        const n = ev || fallback;
                        room.on(n, fn);
                        out.listeners.push(n);
                    };
                    if (withLevels) {
                        let buf = [];
                        on(events.TRACK_AUDIO_LEVEL_CHANGED, 'conference.audioLevelsChanged',
                           (id, level) => {
                               if (String(id) !== myId) buf.push([String(id), Number(level) || 0]);
                           });
                        setInterval(() => {
                            if (buf.length) { const b = buf; buf = []; window.botOnAudioLevels(b); }
                        }, 250);
                    }
                    on(events.USER_JOINED, 'conference.userJoined',
                       (id, user) => window.botOnParticipant('join', String(id), name(user)));
                    on(events.USER_LEFT, 'conference.userLeft',
                       (id) => window.botOnParticipant('leave', String(id), ''));
                    on(events.DISPLAY_NAME_CHANGED, 'conference.displayNameChanged',
                       (id, n) => window.botOnParticipant('rename', String(id), String(n || '')));
                } catch (e) {
                    out.errors.push(String(e));
                }
                return out;
            }""",
            levels,
        )
        log.info(
            "Ponte do modo assist instalada: ouvintes=%s erros=%s",
            diag.get("listeners"), diag.get("errors"),
        )
        return diag

    def on_audio_levels(self, callback) -> None:
        """callback(items: list[tuple[pid, nível]]) — lote de níveis de áudio."""
        self._level_listeners.append(callback)

    def on_participant(self, callback) -> None:
        """callback(kind: 'join'|'leave'|'rename', pid, name) — pode ser corrotina."""
        self._participant_listeners.append(callback)

    def on_dominant_speaker(self, callback) -> None:
        self._speaker_listeners.append(callback)

    def on_chat_message(self, callback) -> None:
        """callback(participant_id: str, text: str, private: bool) — pode ser corrotina."""
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

    async def send_private(self, participant_id: str, message: str) -> None:
        """Mensagem privada; sem suporte na página, cai no chat público.

        Nunca passe conteúdo sensível (ex.: o código de admin) por aqui.
        """
        sent = False
        if self.can_send_private is not False:
            sent = await self.page.evaluate(
                """([id, msg]) => {
                    const room = APP.conference._room;
                    if (typeof room.sendPrivateTextMessage !== 'function') return false;
                    room.sendPrivateTextMessage(id, msg);
                    return true;
                }""",
                [participant_id, message],
            )
        if not sent:
            log.warning("Mensagem privada indisponível — respondendo no chat público")
            await self.send_chat(message)

    async def hangup(self) -> None:
        try:
            await self.page.evaluate("() => APP.conference.hangup()")
        except Exception:
            pass
