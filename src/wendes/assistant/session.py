"""`AssistantSession`: orquestra o modo assist durante a reunião.

Liga a captura à transcrição contínua, os eventos da sala ao `MeetingState`,
os comandos do chat às funcionalidades (perguntas, notas, artefatos, ações)
e o estado à página ao vivo. Cuida do ciclo de vida: aviso de entrada, pausa,
política de participante desconhecido e encerramento ordenado.
"""
from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path

from wendes.assistant import prompts_ptbr as P
from wendes.assistant.policy import Policy
from wendes.assistant.speakers import SpeakerTracker
from wendes.assistant.state import GAP_TEXT, MeetingState, Segment, mmss
from wendes.assistant.transcriber import ContinuousTranscriber, TranscribeFn, VadFn
from wendes.assistant.triggers import Command, parse_chat
from wendes.config import Config
from wendes.facilitator.auth import AdminAuth

log = logging.getLogger(__name__)

ROSTER_POLL_SECONDS = 15.0
STATUS_TICK_SECONDS = 2.0
SpeakFn = Callable[[str], Awaitable[None]]
SegmentHook = Callable[[Segment], Awaitable[None]]


class AssistantSession:
    def __init__(
        self,
        cfg: Config,
        room,
        capture,
        run_dir: Path,
        *,
        state: MeetingState,
        transcribe: TranscribeFn,
        vad: VadFn,
        speak: SpeakFn | None = None,
        llm=None,
        web: bool = True,
        max_lag_s: float | None = None,
        vad_in_thread: bool = True,
    ) -> None:
        self.cfg = cfg
        self.a = cfg.assistant
        self.room = room
        self.capture = capture
        self.run_dir = run_dir
        self.state = state
        self.speak_fn = speak
        self.llm = llm
        self.auth = AdminAuth(cfg, room, run_dir)
        self.policy = Policy(cfg, self.auth)
        self.finished = asyncio.Event()
        self.end_reason = ""
        self.segment_hooks: list[SegmentHook] = []
        # esperas de trabalho de fundo (ex.: fila do LLM) usadas por `idle()`
        self.idle_hooks: list[Callable[[], Awaitable[None]]] = []
        self._tasks: list[asyncio.Task] = []
        self._speak_lock = asyncio.Lock()
        self._unknown: dict[str, str] = {}
        self._seen: dict[str, str] = {}  # todos que passaram pela sala (pid -> nome)
        self._auto_paused = False
        self._pause_start = 0.0
        self._paused_by = ""
        self.bot_name = cfg.room.bot_display_name

        self.tracker: SpeakerTracker | None = None
        if self.a.speakers.enabled:
            from wendes.facilitator.stealth import canonical_name

            sp = self.a.speakers
            self.tracker = SpeakerTracker(
                lambda: capture.position_s,
                latency_offset_s=sp.latency_offset_s,
                min_share=sp.min_share,
                min_samples=sp.min_samples,
                log_path=run_dir / "audio_levels.jsonl",
                name_fn=lambda name: canonical_name(cfg, name),
            )
            room.on_audio_levels(self.tracker.on_levels)
            room.on_dominant_speaker(self.tracker.on_dominant)

        self.audio_q = capture.subscribe(maxsize=240, timed=True)
        st = self.a.stt
        self.transcriber = ContinuousTranscriber(
            self.audio_q,
            transcribe=transcribe,
            vad=vad,
            position_fn=lambda: capture.position_s,
            on_segment=self._on_transcribed,
            on_gap=self._on_gap,
            silence_s=st.silence_s,
            max_utterance_s=st.max_utterance_s,
            max_lag_s=st.max_lag_s if max_lag_s is None else max_lag_s,
            vad_in_thread=vad_in_thread,
        )
        room.on_chat_message(self.handle_chat)
        room.on_participant(self._on_participant)

        self.web = None
        if web:
            from wendes.assistant.web.server import WebServer

            w = self.a.web
            self.web = WebServer(
                state,
                host=w.host,
                port=w.port,
                public_base_url=w.public_base_url,
                allow_edit=w.allow_edit,
            )

    # ---- ciclo de vida ---------------------------------------------------
    @property
    def paused(self) -> bool:
        return self.state.status == "paused"

    def attendees(self) -> list[str]:
        return list(dict.fromkeys(self._seen.values()))

    @property
    def unknown_present(self) -> bool:
        return bool(self._unknown)

    def spawn(self, coro, name: str) -> asyncio.Task:
        task = asyncio.create_task(coro, name=name)
        self._tasks.append(task)
        return task

    async def start(self) -> None:
        if self.web is not None:
            try:
                await self.web.start()
                self._write_link()
            except OSError:
                log.exception("Falha ao abrir a página na porta %s — seguindo sem ela",
                              self.a.web.port)
                self.web = None
        await self.auth.publish_code()
        self.spawn(self.transcriber.run(), "transcriber")
        self.spawn(self._poll_roster(), "roster")
        self.spawn(self._status_ticker(), "status")
        await self.greet()

    def _write_link(self) -> None:
        assert self.web is not None
        path = self.run_dir / "page_link.txt"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(self.web.page_url() + "\n")

    async def greet(self) -> None:
        if not self.a.greeting:
            return
        await self.send_chat(self.a.greeting)
        if self.a.speak_greeting:
            await self.say(self.a.greeting)

    def end(self, reason: str = "") -> None:
        if not self.finished.is_set():
            self.end_reason = reason
            log.info("Encerrando a sessão assist (%s)", reason or "pedido")
            self.finished.set()

    async def wait(self, max_hours: float) -> None:
        try:
            await asyncio.wait_for(self.finished.wait(), timeout=max_hours * 3600)
        except asyncio.TimeoutError:
            log.warning("Limite de %.1fh atingido sem /fim — encerrando", max_hours)
            self.end("limite de tempo")

    async def idle(self) -> None:
        """Espera o trabalho de fundo pendente terminar (usado no fim do replay)."""
        for hook in self.idle_hooks:
            await hook()

    async def stop(self) -> None:
        self.state.set_status("finished")
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self.capture.unsubscribe(self.audio_q)
        if self.tracker is not None:
            self.tracker.close()
        if self.web is not None:
            await self.web.stop()

    # ---- chat e voz --------------------------------------------------------
    async def send_chat(self, text: str) -> None:
        try:
            await self.room.send_chat(text)
        except Exception:  # noqa: BLE001 — chat é acessório
            log.warning("Falha ao enviar mensagem no chat", exc_info=True)

    async def reply(self, pid: str, private: bool, text: str) -> None:
        try:
            if private:
                await self.room.send_private(pid, text)
            else:
                await self.room.send_chat(text)
        except Exception:  # noqa: BLE001
            log.warning("Falha ao responder no chat", exc_info=True)

    async def say(self, text: str) -> None:
        """Fala pelo TTS (desmuta, fala, muta) e registra a fala na transcrição."""
        if self.speak_fn is None or not text.strip():
            return
        async with self._speak_lock:
            start = self.capture.position_s
            try:
                await self.speak_fn(text)
            except Exception:  # noqa: BLE001 — voz é acessória
                log.warning("Falha ao falar pelo TTS", exc_info=True)
                return
            end = max(self.capture.position_s, start + len(text) / 15)
            self.state.add_segment(start, end, text, speaker=self.bot_name, source="bot")

    async def display_name(self, pid: str) -> str:
        name = self.state.participants.get(pid)
        if name:
            return name
        try:
            return await self.room.get_display_name(pid)
        except Exception:  # noqa: BLE001
            return ""

    # ---- transcrição ------------------------------------------------------
    async def _on_transcribed(self, start: float, end: float, text: str) -> None:
        speaker, conf = ("", 0.0)
        if self.tracker is not None:
            speaker, conf = self.tracker.attribute(start, end)
        seg = self.state.add_segment(start, end, text, speaker=speaker, speaker_conf=conf)
        log.info("Segmento %d (%.1f–%.1f s, %d caracteres)", seg.id, start, end, len(text))
        log.debug("Segmento %d: %s", seg.id, text)
        for hook in self.segment_hooks:
            try:
                await hook(seg)
            except Exception:  # noqa: BLE001 — um gancho não derruba a transcrição
                log.exception("Falha num gancho de segmento")

    async def _on_gap(self, start: float, end: float) -> None:
        self.state.add_segment(start, end, GAP_TEXT, marker=True)

    async def _status_ticker(self) -> None:
        while True:
            await asyncio.sleep(STATUS_TICK_SECONDS)
            self.state.stt_lag = self.transcriber.lag
            self.state.position = self.capture.position_s
            self.state.publish_status()

    # ---- participantes ------------------------------------------------------
    async def _on_participant(self, kind: str, pid: str, name: str = "") -> None:
        parts = dict(self.state.participants)
        if kind == "leave":
            parts.pop(pid, None)
        elif name:
            parts[pid] = name
            if self.tracker is not None:
                self.tracker.set_name(pid, name)
        self.state.set_participants(parts)
        await self.check_unknown()

    async def _poll_roster(self) -> None:
        while True:
            try:
                parts = {
                    p.id: p.display_name
                    for p in await self.room.list_participants()
                    if p.display_name.strip() and not self.policy.is_bot(p.display_name)
                }
                self.state.set_participants(parts)
                if self.tracker is not None:
                    for pid, name in parts.items():
                        self.tracker.set_name(pid, name)
                await self.check_unknown()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — roster é acessório
                log.debug("Falha ao listar participantes", exc_info=True)
            await asyncio.sleep(ROSTER_POLL_SECONDS)

    async def check_unknown(self) -> None:
        self._seen.update(self.state.participants)
        mode = self.a.unknown_participant
        current = {
            pid: name
            for pid, name in self.state.participants.items()
            if self.policy.is_unknown(pid, name)
        }
        new = [pid for pid in current if pid not in self._unknown]
        self._unknown = current
        if mode == "allow":
            return
        for pid in new:
            name = current[pid]
            log.info("Participante fora do roster entrou (%s)", pid)
            if mode == "warn":
                await self.send_chat(P.UNKNOWN_WARN.format(nome=name))
            elif mode == "pause":
                if not self.paused:
                    await self.pause(name, auto=True)
            elif mode == "leave":
                await self.send_chat(P.UNKNOWN_LEAVE.format(nome=name))
                self.end("participante desconhecido")
                return
        if mode == "pause" and not current and self.paused and self._auto_paused:
            await self.resume()

    # ---- pausa ---------------------------------------------------------------
    async def pause(self, by: str, auto: bool = False) -> None:
        if self.paused:
            return
        self.transcriber.flush()  # o que já foi dito antes da pausa ainda é transcrito
        self.capture.paused = True
        if self.tracker is not None:
            self.tracker.paused = True
        self._pause_start = self.capture.position_s
        self._auto_paused = auto
        self._paused_by = by
        self.state.set_status("paused", by)
        log.info(
            "Gravação pausada em %.1f s (%s)", self._pause_start, "automática" if auto else "chat"
        )
        await self.send_chat((P.PAUSED_AUTO if auto else P.PAUSED).format(nome=by))

    async def resume(self) -> None:
        if not self.paused:
            return
        end = self.capture.position_s
        self.capture.paused = False
        if self.tracker is not None:
            self.tracker.paused = False
        self._auto_paused = False
        text = P.PAUSE_MARKER.format(
            nome=self._paused_by, ini=mmss(self._pause_start), fim=mmss(end)
        )
        self.state.add_segment(self._pause_start, end, text, marker=True)
        self.state.set_status("recording")
        log.info("Gravação retomada em %.1f s", end)
        await self.send_chat(P.RESUMED)

    # ---- comandos --------------------------------------------------------------
    async def handle_chat(self, pid: str, text: str, private: bool = False) -> None:
        cmd = parse_chat(text, self.bot_name, private)
        if cmd is None:
            return
        if cmd.name == "auth":
            await self.auth.handle_auth(pid, cmd.arg, private)
            return
        display = await self.display_name(pid)
        handler = getattr(self, f"cmd_{cmd.name}", None)
        if handler is None:
            await self.reply(pid, private, P.NOT_AVAILABLE)
            return
        try:
            await handler(pid, display, cmd, private)
        except Exception:  # noqa: BLE001 — um comando ruim não derruba a reunião
            log.exception("Falha ao tratar o comando %s", cmd.name)

    async def deny(self, pid: str, display: str, private: bool, acao: str) -> None:
        hint = P.ADMIN_ONLY_HINT if self.auth.require_code else ""
        await self.reply(pid, private, P.NO_PERMISSION.format(
            nome=display or "Participante", acao=acao) + hint)

    async def cmd_desconhecido(self, pid, display, cmd: Command, private) -> None:
        await self.reply(pid, private, P.UNKNOWN_COMMAND.format(cmd=cmd.arg))

    async def cmd_ajuda(self, pid, display, cmd, private) -> None:
        await self.reply(pid, private, P.help_text(
            ask=self.policy.can_ask(pid, display),
            artifacts=self.policy.can_artifacts(pid, display),
            admin=self.policy.is_admin(pid, display),
        ))

    async def cmd_pausar(self, pid, display, cmd, private) -> None:
        if self.paused:
            await self.reply(pid, private, P.ALREADY_PAUSED)
            return
        await self.pause(display or "um participante")  # a privacidade vence: qualquer um

    async def cmd_retomar(self, pid, display, cmd, private) -> None:
        if not self.policy.is_admin(pid, display):
            await self.deny(pid, display, private, "retomar a gravação")
            return
        if not self.paused:
            await self.reply(pid, private, P.NOT_PAUSED)
            return
        await self.resume()

    async def cmd_link(self, pid, display, cmd, private) -> None:
        if not self.policy.is_admin(pid, display):
            await self.deny(pid, display, private, "pedir o link da página")
            return
        if self.web is None:
            await self.reply(pid, private, P.LINK_UNAVAILABLE)
            return
        if not private or getattr(self.room, "can_send_private", None) is False:
            await self.reply(pid, private, P.LINK_PUBLIC_REFUSED)
            return
        await self.room.send_private(pid, P.LINK_PRIVATE.format(url=self.web.page_url()))

    async def cmd_fim(self, pid, display, cmd, private) -> None:
        if not self.policy.is_admin(pid, display):
            await self.deny(pid, display, private, "encerrar a reunião")
            return
        await self.send_chat(P.END)
        self.end(f"/fim de {display or pid}")
