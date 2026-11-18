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

from pydantic import BaseModel

from wendes.assistant import prompts_ptbr as P
from wendes.assistant.llm_queue import HUMAN, LlmScheduler, ask_json, est
from wendes.assistant.policy import Policy
from wendes.assistant.speakers import SpeakerTracker
from wendes.assistant.state import GAP_TEXT, MeetingState, Segment, mmss
from wendes.assistant.transcriber import ContinuousTranscriber, TranscribeFn, VadFn
from wendes.assistant.triggers import Command, VoiceTrigger, parse_chat
from wendes.config import Config
from wendes.facilitator.auth import AdminAuth

log = logging.getLogger(__name__)

ROSTER_POLL_SECONDS = 15.0
THINKING_AFTER_S = 2.0  # passou disso sem resposta: sinal imediato


class Recap(BaseModel):
    texto: str
    falado: str = ""

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
        screen=None,
    ) -> None:
        self.cfg = cfg
        self.screen = screen  # F10: jitsi.screen.ScreenShare (ou fake nos testes)
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

        # ---- LLM: fila única com prioridade, notas vivas -----------------
        self.system = P.system(self.bot_name, cfg.team.name)
        self.scheduler = LlmScheduler(
            cfg.llm.max_concurrency,
            timeout_s=max(60.0, cfg.llm.timeout_seconds * 3.0),
            telemetry_path=run_dir / "llm_calls.jsonl" if state.persist else None,
            on_change=self._on_llm_queue,
        )
        self.idle_hooks.append(self.scheduler.join)
        self.notes = None
        if llm is not None:
            from wendes.assistant.notes import NotesUpdater

            n = self.a.notes
            self.notes = NotesUpdater(
                state, self.scheduler, llm,
                system=self.system,
                context_tokens=cfg.llm.context_tokens,
                interval_s=n.interval_s,
                min_new_chars=n.min_new_chars,
                compact_every_s=n.compact_every_s,
                structured_mode=cfg.llm.structured_output,
                vllm_key=cfg.llm.vllm_guided_key,
                owner_names={m.name for m in cfg.team.members},
                min_conf=self.a.speakers.min_share,
            )
            self.segment_hooks.append(self.notes.on_segment)

        # ---- perguntas por voz ---------------------------------------------
        self.voice: VoiceTrigger | None = None
        if self.a.voice_trigger:
            wake = [self.a.wake_word or self.bot_name, *self.a.wake_word_aliases]
            self.voice = VoiceTrigger(wake)
            self.segment_hooks.append(self._voice_hook)
        self._requests: set[asyncio.Task] = set()

        # ---- artefatos (F4) -----------------------------------------------------
        from wendes.assistant.artifacts import ArtifactStore

        self.artifacts = ArtifactStore(run_dir if state.persist else None)
        if self.artifacts.items:
            state.artifacts = self.artifacts.public()
        self._repaired: set[tuple[int, int]] = set()

        # ---- pendências de reuniões anteriores (F5) -------------------------
        self.prev_pending: list = []
        if self.notes is not None:
            self.notes.on_change = self._on_notes_change

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
                on_edit=self.edit_artifact,
                on_render_error=self.render_error,
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
        await self.load_pending()
        self.scheduler.start()
        if self.notes is not None:
            self.spawn(self.notes.loop(), "notes")
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

    def _on_llm_queue(self, size: int) -> None:
        self.state.llm_queue = size

    async def stop(self) -> None:
        for task in list(self._requests):
            task.cancel()
        await asyncio.gather(*self._requests, return_exceptions=True)
        if self.notes is not None:
            await self.notes.final_round()  # o que sobrou da transcrição ao vivo
            await self.notes.close()
        await self.scheduler.close()
        self.state.set_status("finished")
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self.capture.unsubscribe(self.audio_q)
        if self.screen is not None and self.screen.active:
            try:
                await self.screen.stop()
            except Exception:  # noqa: BLE001
                log.warning("Falha ao parar o compartilhamento de tela", exc_info=True)
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

    async def cmd_tela(self, pid, display, cmd, private) -> None:
        if not self.policy.is_admin(pid, display):
            await self.deny(pid, display, private, "compartilhar a tela")
            return
        if not self.a.screen_share or self.screen is None:
            await self.reply(pid, private, P.SCREEN_DISABLED)
            return
        if cmd.arg.strip().lower() in ("off", "parar", "sair", "desligar"):
            if not self.screen.active:
                await self.reply(pid, private, P.SCREEN_NOT_ACTIVE)
                return
            await self.screen.stop()
            await self.send_chat(P.SCREEN_OFF)
            return
        if self.web is None:
            await self.reply(pid, private, P.LINK_UNAVAILABLE)
            return
        try:
            ok = await self.screen.start(self.web.local_url())
        except Exception:  # noqa: BLE001
            log.warning("Falha ao compartilhar a tela", exc_info=True)
            ok = False
        await self.send_chat(P.SCREEN_ON if ok else P.SCREEN_FAILED)

    # ---- LLM: utilitários -------------------------------------------------
    async def with_thinking(self, fut, *, voice: bool, pid: str, private: bool):
        """Espera o resultado; passando de 2 s, dá um sinal imediato (chat ou voz)."""
        task = asyncio.ensure_future(fut)
        done, _ = await asyncio.wait({task}, timeout=THINKING_AFTER_S)
        if not done:
            if voice and self.a.voice_answers and self.speak_fn is not None:
                await self.say(P.THINKING_VOICE)
            else:
                await self.reply(pid, private, P.THINKING_CHAT)
        return await task

    def chat_text(self, text: str) -> str:
        if len(text) > P.CHAT_LIMIT:
            return text[: P.CHAT_LIMIT].rstrip() + P.TRUNCATED
        return text

    # ---- resumo e ações (F6) ---------------------------------------------
    async def cmd_resumo(self, pid, display, cmd, private, voice: bool = False) -> None:
        if not voice and not self.policy.can_ask(pid, display):
            await self.deny(pid, display, private, "pedir o resumo")
            return
        from wendes.assistant.notes import Notes, full_view, recap_fallback

        notes = self.notes.notes if self.notes is not None else Notes()
        who = display or "alguém na sala (não identificado)"
        header = f"↳ {who}: resumo até agora"
        if self.llm is None:
            await self.reply(pid, private, f"{header}\n{recap_fallback(notes)}\n({P.LLM_DOWN})")
            return
        prompt = P.RECAP.format(notas="\n".join(full_view(notes)) or "(ainda sem notas)")

        def call():
            return ask_json(
                self.llm, prompt, Recap, system=self.system,
                mode=self.cfg.llm.structured_output, vllm_key=self.cfg.llm.vllm_guided_key,
                max_tokens=500,
            )

        fut = self.scheduler.submit(HUMAN, "resumo", call, est_tokens=est(prompt, 500))
        try:
            recap = await self.with_thinking(fut, voice=voice, pid=pid, private=private)
        except Exception:  # noqa: BLE001
            recap = None
        if recap is None:
            await self.reply(pid, private, f"{header}\n{recap_fallback(notes)}")
            return
        await self.reply(pid, private, self.chat_text(f"{header}\n{recap.texto}"))
        if voice and self.a.voice_answers:
            await self.say(recap.falado)

    async def cmd_acoes(self, pid, display, cmd, private) -> None:
        if not self.policy.can_ask(pid, display):
            await self.deny(pid, display, private, "ver as ações")
            return
        from wendes.assistant.notes import Notes, actions_text

        notes = self.notes.notes if self.notes is not None else Notes()
        await self.reply(pid, private, self.chat_text(actions_text(notes)))

    # ---- perguntas e pedidos (F3) -----------------------------------------
    def spawn_request(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._requests.add(task)

        def done(t: asyncio.Task) -> None:
            self._requests.discard(t)
            if not t.cancelled() and t.exception() is not None:
                log.error("Falha num pedido ao assistente", exc_info=t.exception())

        task.add_done_callback(done)
        return task

    async def _voice_hook(self, seg: Segment) -> None:
        if self.voice is None or seg.source == "bot" or seg.marker:
            return
        req = self.voice.check(seg.start, seg.end, seg.text, seg.speaker)
        if req is None or not self.policy.can_voice_ask():
            return
        log.info("Ativação por voz no segmento %d", seg.id)
        self.spawn_request(
            self.handle_request(req.text, pid="", display=seg.speaker, private=False, voice=True)
        )

    async def cmd_ask(self, pid, display, cmd: Command, private) -> None:
        if not self.policy.can_ask(pid, display):
            await self.deny(pid, display, private, "fazer perguntas ao assistente")
            return
        if not cmd.arg.strip():
            await self.reply(pid, private, P.ASK_USAGE)
            return
        await self.handle_request(cmd.arg, pid=pid, display=display, private=private, voice=False)

    async def route(self, text: str) -> str:
        from wendes.assistant.qa import route_keywords, route_llm

        intent = route_keywords(text)
        if intent is not None or self.llm is None:
            return intent or "pergunta"
        llm_cfg = self.cfg.llm
        fut = self.scheduler.submit(
            HUMAN, "roteador",
            lambda: route_llm(self.llm, text, system=self.system,
                              mode=llm_cfg.structured_output, vllm_key=llm_cfg.vllm_guided_key),
            est_tokens=est(text, 30),
        )
        try:
            return await fut
        except Exception:  # noqa: BLE001
            return "pergunta"

    async def handle_request(self, text: str, *, pid: str, display: str, private: bool,
                             voice: bool) -> None:
        intent = await self.route(text)
        log.info("Pedido roteado como %s (%s)", intent, "voz" if voice else "chat")
        if intent == "resumo":
            await self.cmd_resumo(pid, display, Command("resumo"), private, voice=voice)
        elif intent == "acoes":
            await self.cmd_acoes(pid, display, Command("acoes"), private)
        elif intent in ("documento", "diagrama", "cronograma", "checklist", "tabela") and hasattr(
            self, "request_artifact"
        ):
            await self.request_artifact(
                intent, text, pid=pid, display=display, private=private, voice=voice
            )
        else:
            await self.ask_question(text, pid=pid, display=display, private=private, voice=voice)

    def _qa_context(self, question: str, history_ok: bool):
        """Monta o contexto (roda na thread do LLM: inclui consulta ao SQLite)."""
        from wendes.assistant.budget import estimate_tokens
        from wendes.assistant.notes import Notes, compact_view
        from wendes.assistant.qa import QAContext, history_context, mentions_past
        from wendes.assistant.search import open_db, search_segments

        notes = self.notes.notes if self.notes is not None else Notes()
        segs = [s for s in list(self.state.segments) if not s.marker]
        cap = int(self.cfg.llm.context_tokens * 0.35)
        recent, used = [], 0
        for s in reversed(segs):
            line = s.line("não identificado")
            used += estimate_tokens(line) + 1
            if used > cap:
                break
            recent.append(s)
        recent.reverse()
        first_recent = recent[0].id if recent else None
        blocks = search_segments(segs, question, skip_from_id=first_recent)
        retrieved = [s.line("não identificado") for b in blocks for s in b]
        history: list[str] = []
        if history_ok and (mentions_past(question) or not blocks):
            try:
                db = open_db(self.cfg.resolve(self.cfg.data_dir))
                try:
                    history = history_context(db, question)
                finally:
                    db.close()
            except Exception:  # noqa: BLE001 — histórico é acessório
                log.warning("Falha ao consultar o histórico", exc_info=True)
        return QAContext(
            notes=compact_view(notes, last_points=6),
            recent=[s.line("não identificado") for s in recent],
            retrieved=retrieved,
            history=history,
        )

    async def ask_question(self, question: str, *, pid: str, display: str, private: bool,
                           voice: bool) -> dict | None:
        from wendes.assistant.qa import answer, clean_citations, mentions_past, short_question

        who = display or ("alguém na sala (não identificado)" if voice else "Participante")
        ts = self.capture.position_s
        if self.llm is None:
            await self.reply(pid, private, P.LLM_DOWN)
            return None
        history_ok = self.a.permissions.history != "none" and self.policy.can_history(
            pid, display, via_voice=voice, unknown_present=self.unknown_present
        )
        blocked = ""
        if mentions_past(question) and not history_ok:
            motivo = (P.HISTORY_VOICE if voice else
                      P.HISTORY_UNKNOWN if self.unknown_present else P.HISTORY_NO_PERMISSION)
            blocked = "\n" + P.HISTORY_BLOCKED.format(motivo=motivo)
        llm_cfg = self.cfg.llm

        def job():
            ctx = self._qa_context(question, history_ok)
            return answer(
                self.llm, question, who, ctx, context_tokens=llm_cfg.context_tokens,
                system=self.system, mode=llm_cfg.structured_output,
                vllm_key=llm_cfg.vllm_guided_key,
            )

        fut = self.scheduler.submit(HUMAN, "pergunta", job,
                                    est_tokens=llm_cfg.context_tokens)
        try:
            out = await self.with_thinking(fut, voice=voice, pid=pid, private=private)
        except Exception:  # noqa: BLE001
            log.warning("Pergunta sem resposta do LLM", exc_info=True)
            out = None
        if out is None:
            await self.reply(pid, private, P.NO_ANSWER)
            if voice and self.a.voice_answers:
                await self.say(P.NO_ANSWER)
            return None
        max_ts = self.capture.position_s or None
        item = {
            "id": len(self.state.qa) + 1,
            "ts": round(ts, 1),
            "quem": who,
            "pergunta": question,
            "via": "voz" if voice else ("privado" if private else "chat"),
            "privada": private,
            "resposta": out.resposta.strip(),
            "resposta_curta": out.resposta_curta.strip(),
            "citacoes": clean_citations(out, max_ts),
        }
        self.state.add_qa(item)
        log.info("Pergunta %d respondida (%s, %d caracteres)", item["id"], item["via"],
                 len(item["resposta"]))
        await self.reply(
            pid, private,
            self.chat_text(f"↳ {who}: {short_question(question)}\n{item['resposta']}{blocked}"),
        )
        if voice and self.a.voice_answers:
            await self.say(item["resposta_curta"] or item["resposta"][:200])
        return item

    # ---- pendências (F5) ----------------------------------------------------
    def _query_pending(self) -> list[dict]:
        from wendes.assistant.actions import ActionStore

        store = ActionStore(self.cfg.resolve(self.cfg.data_dir) / "history.sqlite")
        try:
            return store.open_items(
                self.state.room_url or self.cfg.room.url,
                date=self.state.date,
                started_at=self.state.started_at,
                lookback_days=self.cfg.actions.lookback_days,
            )
        finally:
            store.close()

    async def load_pending(self) -> None:
        from wendes.assistant.actions import pending_chat, to_pending

        if not self.state.date:
            return
        try:
            rows = await asyncio.to_thread(self._query_pending)
        except Exception:  # noqa: BLE001 — pendências são acessórias
            log.exception("Falha ao carregar as pendências anteriores")
            return
        items = to_pending(rows)
        self._set_pending(items)
        log.info("%d pendência(s) anterior(es) carregada(s)", len(items))
        if items:
            await self.send_chat(pending_chat(items))

    def _set_pending(self, items: list) -> None:
        self.prev_pending = items
        if self.notes is not None:
            self.notes.set_prev_pending(items)  # dispara _on_notes_change
        else:
            self._publish_pending(items)

    def _on_notes_change(self, notes) -> None:
        self.prev_pending = notes.pendencias_anteriores
        self._publish_pending(notes.pendencias_anteriores)

    def _publish_pending(self, items: list) -> None:
        import json

        from wendes.assistant.actions import page_items

        data = page_items(items)
        if data != self.state.prev_actions:
            self.state.set_prev_actions(data)
            if self.state.persist:
                self.state.path("prev_actions.json").write_text(
                    json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
                )

    async def cmd_pendencias(self, pid, display, cmd, private) -> None:
        from wendes.assistant.actions import pending_chat

        if not self.policy.can_ask(pid, display):
            await self.deny(pid, display, private, "ver as pendências")
            return
        await self.reply(pid, private, self.chat_text(pending_chat(self.prev_pending)))

    async def cmd_cobrar(self, pid, display, cmd, private) -> None:
        """Lê por voz as pendências com responsável, escuta 20 s e classifica."""
        if not self.policy.is_admin(pid, display):
            await self.deny(pid, display, private, "cobrar as pendências")
            return
        targets = [p for p in self.prev_pending
                   if p.responsavel and p.status not in ("concluida", "cancelada")]
        if not targets or self.speak_fn is None:
            await self.reply(pid, private, P.COBRAR_NONE if not targets else P.NOT_AVAILABLE)
            return
        self.spawn_request(self._cobrar(targets))

    async def _cobrar(self, targets: list, listen_s: float = 20.0,
                      settle_s: float | None = None) -> None:
        from wendes.assistant.actions import weekday_pt
        from wendes.assistant.notes import Op, apply_ops

        await self.send_chat(P.COBRAR_START.format(n=len(targets)))
        for p in targets:
            await self.say(P.COBRAR_ASK.format(
                nome=p.responsavel, descricao=p.descricao, dia=weekday_pt(p.meeting_date)))
            start = self.capture.position_s
            while self.capture.position_s < start + listen_s and not self.finished.is_set():
                await asyncio.sleep(0.5)
            # espera o STT ao vivo alcançar o fim da resposta
            await asyncio.sleep(min(5.0, self.a.stt.silence_s + 1.0) if settle_s is None
                                else settle_s)
            segs = [s for s in self.state.segments
                    if s.start >= start and s.source != "bot" and not s.marker]
            status = "sem_resposta"
            if segs and self.llm is not None:
                status = await self._classify_reply(p.descricao, segs)
            if status in ("concluida", "mencionada") and self.notes is not None:
                op = Op(op="update_action", id=p.id, status=status, ts=segs[0].start)
                apply_ops(self.notes.notes, [op])
                self.notes.save([{"op": "update_action", "id": p.id, "status": status,
                                  "via": "/cobrar"}])
            resultado = {"concluida": "resolvida ✔", "aberta": "ainda aberta",
                         "mencionada": "ainda aberta"}.get(status, "sem resposta")
            await self.send_chat(P.COBRAR_RESULT.format(
                ref=p.id, descricao=p.descricao, resultado=resultado))

    async def _classify_reply(self, descricao: str, segs: list) -> str:
        from typing import Literal

        class Classif(BaseModel):
            status: Literal["concluida", "aberta", "sem_resposta"] = "sem_resposta"

        prompt = P.COBRAR_CLASSIFY.format(
            descricao=descricao, trechos="\n".join(s.line("não identificado") for s in segs))
        llm_cfg = self.cfg.llm
        fut = self.scheduler.submit(
            HUMAN, "cobrar",
            lambda: ask_json(self.llm, prompt, Classif, system=self.system,
                             mode=llm_cfg.structured_output, vllm_key=llm_cfg.vllm_guided_key,
                             max_tokens=30),
        )
        try:
            out = await fut
        except Exception:  # noqa: BLE001
            return "sem_resposta"
        if out is None:
            return "sem_resposta"
        return "mencionada" if out.status == "aberta" else out.status

    # ---- artefatos (F4) -------------------------------------------------------
    async def cmd_artifact(self, pid, display, cmd: Command, private) -> None:
        if not self.policy.can_artifacts(pid, display):
            await self.deny(pid, display, private, "criar artefatos")
            return
        if not cmd.arg.strip():
            await self.reply(pid, private, P.ARTIFACT_USAGE)
            return
        await self.make_artifact(cmd.kind, cmd.arg, ref=cmd.ref, pid=pid, display=display,
                                 private=private, voice=False)

    async def request_artifact(self, intent: str, text: str, *, pid: str, display: str,
                               private: bool, voice: bool) -> None:
        """Pedido por texto livre ou voz, já roteado para um tipo de artefato."""
        from wendes.assistant.artifacts import INTENT_TO_TYPE

        if voice:
            allowed = self.a.permissions.artifacts in ("all", "members")
        else:
            allowed = self.policy.can_artifacts(pid, display)
        if not allowed:
            await self.deny(pid, display, private, "criar artefatos")
            return
        await self.make_artifact(INTENT_TO_TYPE.get(intent, "doc"), text, ref=None, pid=pid,
                                 display=display, private=private, voice=voice)

    def _artifact_context(self) -> tuple[str, str]:
        from wendes.assistant.budget import Part, fit
        from wendes.assistant.notes import Notes, compact_view

        notes = self.notes.notes if self.notes is not None else Notes()
        budget = max(800, self.cfg.llm.context_tokens - 1600 - 600)
        parts = fit([
            Part("n", compact_view(notes, last_points=5), int(budget * 0.35), cut_order=1),
            Part("t", [s.line("não identificado") for s in self.state.segments if not s.marker],
                 int(budget * 0.5), keep="newest", cut_order=0),
        ], budget)
        return "\n".join(parts["n"]) or "(sem notas)", "\n".join(parts["t"]) or "(vazia)"

    def _generate_artifact(self, tipo: str, prompt: str):
        """Roda na thread do LLM: gera e valida (uma nova tentativa com o erro)."""
        from wendes.assistant.artifacts import ArtifactOut, validate

        llm_cfg = self.cfg.llm
        current = prompt
        for _ in range(2):
            out = ask_json(self.llm, current, ArtifactOut, system=self.system,
                           mode=llm_cfg.structured_output, vllm_key=llm_cfg.vllm_guided_key,
                           max_tokens=1600)
            if out is None:
                return None
            try:
                return out.titulo, validate(tipo, out.conteudo)
            except ValueError as exc:
                current = f"{prompt}\n\nA resposta anterior foi inválida: {exc}. Corrija."
        return None

    async def make_artifact(self, tipo: str, request: str, *, ref: int | None, pid: str,
                            display: str, private: bool, voice: bool) -> None:
        from wendes.assistant.artifacts import UPDATE_VERBS
        from wendes.assistant.search import _fold

        if self.llm is None:
            await self.reply(pid, private, P.LLM_DOWN)
            return
        target = None
        if ref is not None:
            target = self.artifacts.get(ref)
            if target is None:
                await self.reply(pid, private, P.ARTIFACT_NOT_FOUND.format(id=ref))
                return
        elif UPDATE_VERBS.search(_fold(request)):
            target = self.artifacts.find_by_title(request)
        notas, trechos = self._artifact_context()
        if target is not None:
            tipo = target.tipo
            prompt = P.ARTIFACT_UPDATE.format(
                guia=P.ARTIFACT_GUIDE[tipo], tipo=tipo, titulo=target.titulo,
                atual=target.latest.conteudo, pedido=request, trechos=trechos,
            )
        else:
            prompt = P.ARTIFACT_CREATE.format(
                guia=P.ARTIFACT_GUIDE[tipo], tipo=tipo, pedido=request, notas=notas,
                trechos=trechos,
            )
        fut = self.scheduler.submit(HUMAN, "artefato",
                                    lambda: self._generate_artifact(tipo, prompt),
                                    est_tokens=est(prompt, 1600), timeout=240.0)
        try:
            result = await self.with_thinking(fut, voice=voice, pid=pid, private=private)
        except Exception:  # noqa: BLE001
            log.warning("Falha ao gerar artefato", exc_info=True)
            result = None
        if result is None:
            await self.reply(pid, private, P.ARTIFACT_FAILED)
            return
        titulo, conteudo = result
        autor = self.bot_name
        pedido = f"{display or 'voz'}: {request}"
        if target is None:
            art = self.artifacts.create(tipo, titulo, conteudo, autor=autor, pedido=pedido)
            msg = P.ARTIFACT_CREATED.format(titulo=art.titulo, id=art.id)
            acao = "criei"
        else:
            v = self.artifacts.add_version(target.id, conteudo, autor=autor, pedido=pedido)
            art = target
            msg = P.ARTIFACT_UPDATED.format(titulo=art.titulo, id=art.id, n=v.n)
            acao = "atualizei"
        self.state.set_artifacts(self.artifacts.public())
        log.info("Artefato #%d (%s) versão %d", art.id, art.tipo, len(art.versoes))
        await self.send_chat(msg)
        if (pid and self.web is not None and self.policy.is_admin(pid, display)
                and getattr(self.room, "can_send_private", None) is not False):
            await self.room.send_private(pid, P.ARTIFACT_LINK.format(
                titulo=art.titulo, url=self.web.page_url()))
        if voice and self.a.voice_answers:
            await self.say(P.ARTIFACT_SPOKEN.format(acao=acao, titulo=art.titulo))

    async def edit_artifact(self, aid: int, content: str, author: str) -> dict:
        """Edição pela página (assistant.web.allow_edit): nova versão com o autor."""
        from wendes.assistant.artifacts import validate

        art = self.artifacts.get(aid)
        if art is None:
            raise KeyError(aid)
        text = validate(art.tipo, content)
        v = self.artifacts.add_version(aid, text, autor=f"{author} (página)",
                                       pedido="edição na página")
        self.state.set_artifacts(self.artifacts.public())
        log.info("Artefato #%d editado na página (versão %d)", aid, v.n)
        return {"ok": True, "versao": v.n}

    async def render_error(self, aid: int, version: int, error: str) -> None:
        """Mermaid não renderizou: pede UMA correção ao LLM (prioridade baixa)."""
        from wendes.assistant.llm_queue import BACKGROUND

        art = self.artifacts.get(aid)
        if art is None or self.llm is None or art.formato != "mmd":
            return
        ver = next((v for v in art.versoes if v.n == version), None)
        if ver is None or ver.reparo or (aid, version) in self._repaired:
            return  # já é uma correção (ou já pedida): a página mostra o código
        if version != art.latest.n:
            return
        self._repaired.add((aid, version))
        log.info("Mermaid do artefato #%d v%d não renderizou — pedindo correção", aid, version)
        prompt = P.ARTIFACT_REPAIR.format(erro=error[:300], tipo=art.tipo, titulo=art.titulo,
                                          codigo=ver.conteudo)

        async def run() -> None:
            fut = self.scheduler.submit(BACKGROUND, "reparo_mermaid",
                                        lambda: self._generate_artifact(art.tipo, prompt),
                                        est_tokens=est(prompt, 1600))
            try:
                result = await fut
            except Exception:  # noqa: BLE001
                return
            if result is None or art.latest.n != version:
                return
            self.artifacts.add_version(aid, result[1], autor=f"{self.bot_name} (correção)",
                                       pedido=f"correção automática: {error[:120]}", reparo=True)
            self.state.set_artifacts(self.artifacts.public())

        self.spawn_request(run())

    async def cmd_fim(self, pid, display, cmd, private) -> None:
        if not self.policy.is_admin(pid, display):
            await self.deny(pid, display, private, "encerrar a reunião")
            return
        await self.send_chat(P.END)
        self.end(f"/fim de {display or pid}")
