"""Montagem do modo assist: sessão ao vivo no Jitsi e replay a partir de um WAV."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path

from wendes.assistant.session import AssistantSession, SpeakFn
from wendes.assistant.state import MeetingState
from wendes.assistant.transcriber import TranscribeFn, VadFn
from wendes.config import Config

log = logging.getLogger(__name__)


def run_id_for(date: str, started_at: str, suffix: str = "") -> str:
    rid = f"{date}_{started_at.replace(':', '-')}"
    return f"{rid}_{suffix}" if suffix else rid


def build_session(
    cfg: Config,
    room,
    capture,
    run_dir: Path,
    state: MeetingState,
    *,
    transcribe: TranscribeFn,
    vad: VadFn,
    speak: SpeakFn | None = None,
    llm=None,
    web: bool = True,
    max_lag_s: float | None = None,
    vad_in_thread: bool = True,
    screen=None,
) -> AssistantSession:
    """Cria a sessão e liga as funcionalidades do modo assist."""
    return AssistantSession(
        cfg, room, capture, run_dir,
        state=state, transcribe=transcribe, vad=vad, speak=speak, llm=llm, web=web,
        max_lag_s=max_lag_s, vad_in_thread=vad_in_thread, screen=screen,
    )


def make_room_speaker(cfg: Config, room, tts) -> SpeakFn:
    """Fala na sala: sintetiza (thread), desmuta, toca no microfone virtual e muta."""
    from wendes.audio.playback import play_wav
    from wendes.facilitator.prompts_ptbr import spoken

    async def speak(text: str) -> None:
        wav = await asyncio.to_thread(tts.synthesize, spoken(text, cfg.tts.phonetic))
        await room.unmute()
        try:
            await asyncio.sleep(0.3)  # o Jitsi leva um instante para abrir o áudio
            await play_wav(str(wav))
        finally:
            await room.mute()

    return speak


async def run_replay(
    cfg: Config,
    source_wav: Path,
    out_dir: Path,
    *,
    speed: float,
    transcribe: TranscribeFn,
    vad: VadFn,
    llm=None,
    room=None,
    record=None,
    read_stdin: bool = True,
    web: bool = True,
    levels_path: Path | None = None,
    room_url: str = "",
    finalize: bool = True,
    commands: list[str] | None = None,
) -> AssistantSession:
    """Roda o pipeline do assist sobre um WAV, com a sala no terminal.

    Ao fim do áudio espera a transcrição (e o LLM) esvaziarem; se o stdin já
    acabou (ex.: não interativo), encerra; senão espera /fim.
    """
    from wendes.assistant.replay import (
        OPERATOR_ID,
        LevelReplayer,
        ReplayCapture,
        TerminalRoom,
        terminal_speak,
    )
    from wendes.facilitator.state_machine import MeetingRecord

    out_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    record = record or MeetingRecord(
        date=now.strftime("%Y-%m-%d"), started_at=now.strftime("%H:%M"), mode="assist"
    )
    room = room or TerminalRoom()
    capture = ReplayCapture(source_wav, out_dir / "recording.wav", speed)
    state = MeetingState(
        out_dir,
        run_id=run_id_for(record.date, record.started_at, "replay"),
        date=record.date,
        started_at=record.started_at,
        room_url=room_url,
        bot_name=cfg.room.bot_display_name,
    )
    session = build_session(
        cfg, room, capture, out_dir, state,
        transcribe=transcribe, vad=vad, speak=terminal_speak, llm=llm, web=web,
        max_lag_s=float("inf") if speed == 0 else None,
    )
    session.auth.grant(OPERATOR_ID)
    if levels_path is not None and levels_path.exists() and session.tracker is not None:
        session.tracker.offset = 0.0  # o arquivo já tem o offset descontado
        capture.listeners.append(LevelReplayer(levels_path, session.tracker))

    stdin_eof = asyncio.Event()
    if not read_stdin:
        stdin_eof.set()
    await capture.start()
    await session.start()
    if session.web is not None:
        print(f"Página: {session.web.page_url()}", flush=True)
    stdin_task = None
    if read_stdin:
        stdin_task = asyncio.create_task(room.read_stdin(on_eof=stdin_eof.set))
    for text in commands or []:
        await room.emit_chat(OPERATOR_ID, text, False)

    audio_done = asyncio.create_task(capture.done.wait())
    finished = asyncio.create_task(session.finished.wait())
    await asyncio.wait({audio_done, finished}, return_when=asyncio.FIRST_COMPLETED)
    if not session.finished.is_set():
        await session.transcriber.drain()
        await session.idle()
        log.info("Fim do áudio do replay")
        if stdin_eof.is_set():
            session.end("fim do replay")
        else:
            print("Fim do áudio — mande comandos ou /fim para encerrar.", flush=True)
            await session.finished.wait()
    for task in (audio_done, finished, stdin_task):
        if task is not None:
            task.cancel()
    await session.stop()
    await capture.stop()
    if finalize:
        from wendes.assistant.finalize import finalize_assist

        finalize_assist(
            cfg, None, out_dir, record, out_dir / "recording.wav",
            room_url=room_url, distribute=False, llm=llm, state=session.state,
        )
    return session
