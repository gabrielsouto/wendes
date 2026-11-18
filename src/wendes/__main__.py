"""CLI do wendes.

Comandos:
  check        verifica ambiente (áudio, TTS, LLM, token Firebase)
  join-test    Fase 1: entra na sala, fala uma frase e loga o roster
  run          conduz a daily completa (facilita, transcreve, ata, distribui)
  stealth      entra mudo e só grava/transcreve; /fim no chat encerra e envia a ata
  assist       assistente visível: transcreve, mantém notas, responde e cria artefatos
  resummarize  regera a ata de uma data a partir do histórico, sem reentrar na sala
  ask          pergunta sobre reuniões passadas (mesmo motor do assist, sem Jitsi)
  reindex      indexa o histórico para busca (FTS5)
  send-actions reenvia o webhook de ações de uma reunião assist
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from wendes.config import load_config

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("wendes")


def cmd_check() -> int:
    cfg = load_config()
    ok = True

    from wendes.audio.devices import ensure_devices

    try:
        ensure_devices()
        print("✔ dispositivos de áudio virtuais ok")
    except Exception as exc:  # noqa: BLE001
        print(f"✘ áudio: {exc}")
        ok = False

    try:
        from wendes.speech.tts import Tts

        Tts(cfg).synthesize("Teste de voz do robô da daily.")
        print("✔ Piper TTS ok")
    except Exception as exc:  # noqa: BLE001
        print(f"✘ TTS: {exc}")
        ok = False

    try:
        from wendes.llm.client import Llm

        resp = Llm(cfg).ask("Responda apenas: ok", max_tokens=10)
        print(f"✔ LLM ok — {cfg.llm.base_url} · {cfg.llm.model} ({resp[:40]!r})")
    except Exception as exc:  # noqa: BLE001
        print(f"✘ LLM ({cfg.llm.base_url} · {cfg.llm.model}): {exc}")
        ok = False

    try:
        from wendes.jitsi.browser import JitsiBrowser

        version = asyncio.run(JitsiBrowser(cfg).check())
        print(f"✔ navegador ok (Chromium {version})")
    except Exception as exc:  # noqa: BLE001
        print(f"✘ navegador: {exc}")
        ok = False

    from wendes.speech.stt_batch import resolve_stt_device, stt_cpu_threads

    print(
        f"ℹ STT no dispositivo {resolve_stt_device(cfg)} (stt.device={cfg.stt.device},"
        f" {stt_cpu_threads(cfg)} threads de CPU, compute_type={cfg.stt.compute_type})"
    )

    if cfg.secrets.firebase_refresh_token:
        try:
            from wendes.jitsi.auth import exchange_refresh_token

            exchange_refresh_token(cfg.secrets.firebase_refresh_token)
            print("✔ token Firebase renovado com sucesso (bot pode abrir a sala)")
        except Exception as exc:  # noqa: BLE001
            print(f"✘ token Firebase: {exc}")
            ok = False
    else:
        print("⚠ FIREBASE_REFRESH_TOKEN não configurado — ver docs/OBTER_REFRESH_TOKEN.md")

    from wendes.assistant.checks import check_assist

    print("— modo assist —")
    for good, msg in check_assist(cfg):
        print(("✔ " if good else "✘ ") + msg)
        ok = ok and good
    if not cfg.secrets.wendes_web_token:
        print("ℹ WENDES_WEB_TOKEN vazio: `wendes serve` não sobe (o assist gera token próprio)")

    return 0 if ok else 1


async def _join_test(stay_seconds: int) -> int:
    cfg = load_config()

    from wendes.audio.devices import ensure_devices
    from wendes.audio.playback import play_wav
    from wendes.jitsi.browser import JitsiBrowser
    from wendes.jitsi.entry import EntryError, enter_room
    from wendes.jitsi.room import JitsiRoom
    from wendes.speech.tts import Tts

    ensure_devices()
    tts = Tts(cfg)
    greeting = tts.synthesize(
        "Bom dia, aqui é o robô da daily. Este é um teste de integração."
    )

    browser = JitsiBrowser(cfg)
    try:
        page = await browser.start()
        try:
            guest_url = await enter_room(page, cfg, cfg.secrets.firebase_refresh_token)
        except EntryError as exc:
            log.error("%s", exc)
            return 1
        log.info("== Link para a equipe entrar na daily: %s ==", guest_url)
        room = JitsiRoom(page)
        await room.wait_joined(cfg.room.join_timeout_seconds)
        await room.install_event_bridge()
        from wendes.facilitator.commands import ChatCommands

        commands = ChatCommands(cfg, room)  # permite testar /auth, /ajuda etc. no chat
        await commands.auth.publish_code()
        await room.send_chat("Bot de teste na sala — envie /ajuda para testar os comandos.")
        await room.unmute()
        await asyncio.sleep(2)
        await play_wav(str(greeting))
        participants = await room.list_participants()
        log.info(
            "Participantes na sala: %s",
            [p.display_name or p.id for p in participants] or "(nenhum além do bot)",
        )
        log.info("Permanecendo na sala por %ss para verificação manual…", stay_seconds)
        await asyncio.sleep(stay_seconds)
        await room.hangup()
        return 0
    finally:
        await browser.close()


def _purge(cfg) -> None:
    """Retenção automática no início de cada execução (todos os modos)."""
    from wendes.assistant.policy import purge

    try:
        purge(cfg)
    except Exception:  # noqa: BLE001 — retenção nunca impede a reunião
        log.exception("Falha ao aplicar a retenção de dados")


def _open_run(cfg):
    """Cria a pasta da execução e o run.log; retorna (store, date, started_at, run_dir)."""
    from datetime import datetime

    from wendes.history.store import HistoryStore

    _purge(cfg)
    store = HistoryStore(cfg)
    now = datetime.now()
    date = now.strftime("%Y-%m-%d")
    started_at = now.strftime("%H:%M")
    run_dir = store.run_dir(date, started_at)
    file_handler = logging.FileHandler(run_dir / "run.log", encoding="utf-8")
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    logging.getLogger().addHandler(file_handler)
    return store, date, started_at, run_dir


async def _shutdown(capture, browser) -> None:
    """Para a captura e fecha o navegador, cada um tolerando o próprio erro."""
    try:
        await capture.stop()
    except Exception:  # noqa: BLE001
        log.exception("Falha ao parar a captura de áudio")
    try:
        await browser.close()
    except Exception:  # noqa: BLE001
        log.exception("Falha ao fechar o navegador")


def _mark_interrupted(record, exc: BaseException) -> None:
    log.exception("A reunião falhou no meio — a ata cobre só o que foi gravado")
    record.interrupted = True
    record.error = (str(exc) or type(exc).__name__)[:200]


async def _run() -> int:
    cfg = load_config()

    from wendes.audio.capture import AudioCapture
    from wendes.audio.devices import ensure_devices
    from wendes.facilitator.commands import ChatCommands
    from wendes.facilitator.state_machine import Facilitator, MeetingRecord
    from wendes.jitsi.browser import JitsiBrowser
    from wendes.jitsi.entry import EntryError, enter_room
    from wendes.jitsi.room import JitsiRoom
    from wendes.llm.client import Llm
    from wendes.pipeline import GracefulStop, finalize_meeting, provisional_save
    from wendes.speech.stt_live import LiveStt
    from wendes.speech.tts import Tts

    ensure_devices()
    tts = Tts(cfg)
    stt = LiveStt(cfg)
    try:
        llm = Llm(cfg)
    except Exception:  # noqa: BLE001
        llm = None

    store, date, started_at, run_dir = _open_run(cfg)
    wav_path = run_dir / "recording.wav"
    record = MeetingRecord(date=date, started_at=started_at, mode="run")
    stopper = GracefulStop().install()

    browser = JitsiBrowser(cfg)
    capture = AudioCapture(wav_path)
    joined = False
    try:
        page = await browser.start()
        try:
            guest_url = await enter_room(page, cfg, cfg.secrets.firebase_refresh_token)
        except EntryError as exc:
            log.error("%s", exc)
            return 1
        (run_dir / "guest_link.txt").write_text(guest_url + "\n")
        log.info("Link de convidados: %s", guest_url)
        room = JitsiRoom(page)
        await room.wait_joined(cfg.room.join_timeout_seconds)
        joined = True
        provisional_save(store, record)
        await room.install_event_bridge()
        await room.unmute()
        await capture.start()

        commands = ChatCommands(cfg, room, run_dir)
        await commands.auth.publish_code()
        stopper.on_meeting_stop(commands.request_end)  # SIGTERM = /encerrar
        facilitator = Facilitator(cfg, room, capture, tts, stt, llm, commands)
        facilitator.record = record  # mesmo objeto: o parcial sobrevive a falhas
        await facilitator.run()
        await room.hangup()
    except asyncio.CancelledError:
        if not joined:
            log.warning("Encerrado antes de entrar na sala — sem reunião, sem ata")
            return 1
        log.warning("Reunião cancelada — gerando a ata do que foi gravado")
    except Exception as exc:  # noqa: BLE001
        if not joined:
            log.exception("Falha antes de entrar na sala — sem reunião, sem ata")
            return 1
        _mark_interrupted(record, exc)
    finally:
        await _shutdown(capture, browser)

    finalize_meeting(cfg, store, run_dir, record, wav_path, mode="run")
    return 1 if record.interrupted else 0


async def _stealth(max_hours: float, room_url: str | None = None) -> int:
    """Modo stealth: entra mudo, grava e transcreve tudo; /fim encerra e envia a ata."""
    cfg = load_config()

    from wendes.audio.capture import AudioCapture
    from wendes.audio.devices import ensure_devices
    from wendes.facilitator.state_machine import MeetingRecord, PersonRecord
    from wendes.facilitator.stealth import StealthSession, canonical_name, is_bot_display
    from wendes.jitsi.browser import JitsiBrowser
    from wendes.jitsi.entry import EntryError, enter_room
    from wendes.jitsi.room import JitsiRoom
    from wendes.pipeline import GracefulStop, finalize_meeting, provisional_save

    ensure_devices()
    store, date, started_at, run_dir = _open_run(cfg)
    wav_path = run_dir / "recording.wav"
    record = MeetingRecord(date=date, started_at=started_at, mode="stealth")
    stopper = GracefulStop().install()

    browser = JitsiBrowser(cfg)
    capture = AudioCapture(wav_path)  # sem inscritos: só grava o WAV
    session: StealthSession | None = None
    joined = False
    try:
        page = await browser.start()
        try:
            guest_url = await enter_room(
                page, cfg, cfg.secrets.firebase_refresh_token, room_url=room_url
            )
        except EntryError as exc:
            log.error("%s", exc)
            return 1
        (run_dir / "guest_link.txt").write_text(guest_url + "\n")
        log.info("Link de convidados: %s", guest_url)
        room = JitsiRoom(page)
        await room.wait_joined(cfg.room.join_timeout_seconds)
        joined = True
        provisional_save(store, record)
        await room.install_event_bridge()
        await room.mute()  # sem voz; única mensagem de chat é o aviso de gravação
        await room.send_chat(
            "🔴 Gravando em modo escuta — não vou interagir. "
            "Ao final, a ata será gerada e enviada por e-mail. Envie /fim para encerrar."
        )

        session = StealthSession(cfg, room, run_dir)
        await session.auth.publish_code()
        stopper.on_meeting_stop(session.finished.set)  # SIGTERM = /fim
        await capture.start()
        log.info("Gravando em modo stealth — /fim no chat encerra (limite %.1fh)", max_hours)
        await session.wait_finished(max_hours=max_hours)
        await room.hangup()
    except asyncio.CancelledError:
        if not joined:
            log.warning("Encerrado antes de entrar na sala — sem reunião, sem ata")
            return 1
        log.warning("Gravação cancelada — gerando a ata do que foi gravado")
    except Exception as exc:  # noqa: BLE001
        if not joined:
            log.exception("Falha antes de entrar na sala — sem reunião, sem ata")
            return 1
        _mark_interrupted(record, exc)
    finally:
        await _shutdown(capture, browser)

    # attendees → nomes canônicos do roster quando casarem (sem contar o bot)
    seen: set[str] = set()
    for display in (session.attendees.values() if session is not None else ()):
        if is_bot_display(cfg, display):
            continue
        name = canonical_name(cfg, display)
        if name not in seen:
            seen.add(name)
            record.people.append(PersonRecord(name=name, present=True))

    finalize_meeting(cfg, store, run_dir, record, wav_path, mode="stealth", room_url=room_url)
    return 1 if record.interrupted else 0


def _assist_attendees(cfg, record, participants) -> None:
    from wendes.facilitator.state_machine import PersonRecord
    from wendes.facilitator.stealth import canonical_name, is_bot_display

    seen = {p.name for p in record.people}
    for display in participants:
        if not display.strip() or is_bot_display(cfg, display):
            continue
        name = canonical_name(cfg, display)
        if name not in seen:
            seen.add(name)
            record.people.append(PersonRecord(name=name, present=True))


def _screen_share(cfg, page):
    if not cfg.assistant.screen_share:
        return None
    from wendes.jitsi.screen import ScreenShare

    return ScreenShare(page)


async def _assist(max_hours: float, room_url: str | None = None) -> int:
    """Modo assist: entra visível, transcreve, mantém notas e responde no chat/voz."""
    cfg = load_config()

    from wendes.assistant.app import build_session, make_room_speaker, run_id_for
    from wendes.assistant.state import MeetingState
    from wendes.assistant.transcriber import make_whisper_transcriber, silero_vad
    from wendes.audio.capture import AudioCapture
    from wendes.audio.devices import ensure_devices
    from wendes.facilitator.state_machine import MeetingRecord
    from wendes.jitsi.browser import JitsiBrowser
    from wendes.jitsi.entry import EntryError, enter_room
    from wendes.jitsi.room import JitsiRoom
    from wendes.pipeline import GracefulStop, finalize_meeting, provisional_save

    ensure_devices()
    store, date, started_at, run_dir = _open_run(cfg)
    wav_path = run_dir / "recording.wav"
    record = MeetingRecord(date=date, started_at=started_at, mode="assist")
    stopper = GracefulStop().install()

    tts = None
    try:
        from wendes.speech.tts import Tts

        tts = Tts(cfg)
    except Exception:  # noqa: BLE001 — sem voz, o assist segue só no chat
        log.exception("TTS indisponível — o assistente responde só no chat")
    log.info("Carregando o Whisper ao vivo (%s)…", cfg.stt.live_model)
    transcribe = await asyncio.to_thread(make_whisper_transcriber, cfg)
    try:
        from wendes.llm.client import Llm

        llm = Llm(cfg)
    except Exception:  # noqa: BLE001
        log.exception("LLM indisponível — transcrição e página seguem sem notas/perguntas")
        llm = None

    browser = JitsiBrowser(cfg)
    capture = AudioCapture(wav_path)
    session = None
    joined = False
    screen_args: list[str] = []
    if cfg.assistant.screen_share:
        from wendes.jitsi.screen import screen_share_args

        screen_args = screen_share_args()
    try:
        page = await browser.start(extra_args=screen_args)
        try:
            guest_url = await enter_room(
                page, cfg, cfg.secrets.firebase_refresh_token, room_url=room_url
            )
        except EntryError as exc:
            log.error("%s", exc)
            return 1
        (run_dir / "guest_link.txt").write_text(guest_url + "\n")
        log.info("Link de convidados: %s", guest_url)
        room = JitsiRoom(page)
        room.log_text = False  # modo assist: texto só em DEBUG
        await room.wait_joined(cfg.room.join_timeout_seconds)
        joined = True
        provisional_save(store, record)
        await room.install_event_bridge()
        await room.install_assist_bridge(levels=cfg.assistant.speakers.enabled)
        await room.mute()
        state = MeetingState(
            run_dir,
            run_id=run_id_for(date, started_at),
            date=date,
            started_at=started_at,
            room_url=room_url or cfg.room.url,
            bot_name=cfg.room.bot_display_name,
        )
        session = build_session(
            cfg, room, capture, run_dir, state,
            transcribe=transcribe, vad=silero_vad,
            speak=make_room_speaker(cfg, room, tts) if tts is not None else None,
            llm=llm,
            screen=_screen_share(cfg, page),
        )
        stopper.on_meeting_stop(lambda: session.end("sinal de parada"))
        await capture.start()
        await session.start()
        log.info("Modo assist ativo — /fim no chat encerra (limite %.1fh)", max_hours)
        await session.wait(max_hours)
        await room.hangup()
    except asyncio.CancelledError:
        if not joined:
            log.warning("Encerrado antes de entrar na sala — sem reunião, sem ata")
            return 1
        log.warning("Sessão cancelada — gerando a ata do que foi gravado")
    except Exception as exc:  # noqa: BLE001
        if not joined:
            log.exception("Falha antes de entrar na sala — sem reunião, sem ata")
            return 1
        _mark_interrupted(record, exc)
    finally:
        if session is not None:
            try:
                await session.stop()
            except Exception:  # noqa: BLE001
                log.exception("Falha ao encerrar a sessão assist")
        await _shutdown(capture, browser)

    if session is not None:
        _assist_attendees(cfg, record, session.attendees())
    finalize_meeting(cfg, store, run_dir, record, wav_path, mode="assist", room_url=room_url)
    return 1 if record.interrupted else 0


async def _assist_replay(date: str, hora: str | None, speed: float) -> int:
    """Replay do modo assist a partir do recording.wav de uma reunião gravada."""
    cfg = load_config()

    from wendes.assistant.app import run_replay
    from wendes.assistant.transcriber import make_whisper_transcriber, silero_vad
    from wendes.facilitator.state_machine import MeetingRecord
    from wendes.history.store import HistoryStore
    from wendes.pipeline import _make_llm

    store = HistoryStore(cfg)
    record = store.load_record(date, hora)
    if record is None:
        print(f"Nenhuma reunião registrada em {date} {hora or ''}".strip())
        return 1
    src_dir = store.run_dir(date, record.started_at)
    wav = src_dir / "recording.wav"
    if not wav.exists():
        print(f"{wav} não existe (apagado pela retenção?)")
        return 1
    out_dir = src_dir / "replay" / datetime_now_slug()
    out_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(out_dir / "run.log", encoding="utf-8")
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    logging.getLogger().addHandler(file_handler)
    print(f"Replay de {wav} → {out_dir}")
    transcribe = await asyncio.to_thread(make_whisper_transcriber, cfg)
    replay_record = MeetingRecord(
        date=record.date, started_at=record.started_at, mode="assist",
        people=record.people,
    )
    await run_replay(
        cfg, wav, out_dir, speed=speed, transcribe=transcribe, vad=silero_vad,
        llm=_make_llm(cfg), record=replay_record,
        levels_path=src_dir / "audio_levels.jsonl", room_url=cfg.room.url,
    )
    print(f"Pronto: {out_dir / 'ata.md'}")
    return 0


def cmd_ask(question: str, date: str | None, hora: str | None) -> int:
    cfg = load_config()

    from wendes.assistant.qa import offline_answer
    from wendes.assistant.state import mmss
    from wendes.history.store import HistoryStore
    from wendes.llm.client import Llm

    store = HistoryStore(cfg)
    try:
        out, scope = offline_answer(cfg, Llm(cfg), store, question, date, hora)
    except Exception as exc:  # noqa: BLE001
        print(f"LLM indisponível: {exc}")
        return 1
    if out is None:
        print(f"Não consegui responder ({scope}).")
        return 1
    print(f"[{scope}]\n")
    print(out.resposta.strip())
    from wendes.assistant.notes import parse_ts

    cites = []
    for c in out.citacoes:
        ts = parse_ts(c.ts)
        when = mmss(ts) if ts is not None else ""
        cites.append(f"{c.data or ''} {when}".strip())
    if cites:
        print("\nCitações: " + ", ".join(cites))
    return 0


def cmd_send_actions(date: str, hora: str | None) -> int:
    cfg = load_config()

    from wendes.assistant.actions import load_payload, post_actions
    from wendes.history.store import HistoryStore

    url = cfg.distribution.actions_webhook_url
    if not url:
        print("distribution.actions_webhook_url não configurado")
        return 1
    store = HistoryStore(cfg)
    record = store.load_record(date, hora)
    if record is None:
        print(f"Nenhuma reunião registrada em {date} {hora or ''}".strip())
        return 1
    payload = load_payload(store.run_dir(date, record.started_at))
    if payload is None:
        print("Essa reunião não tem actions_payload.json (não foi no modo assist?)")
        return 1
    ok = post_actions(url, payload)
    print("Enviado." if ok else "Falhou — veja o log.")
    return 0 if ok else 1


def cmd_reindex() -> int:
    cfg = load_config()

    from wendes.assistant.search import reindex_all
    from wendes.history.store import HistoryStore

    HistoryStore(cfg)  # garante o schema
    meetings, segs = reindex_all(cfg.resolve(cfg.data_dir))
    print(f"Indexadas {meetings} reuniões ({segs} segmentos)")
    return 0


def datetime_now_slug() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y%m%d-%H%M%S")


def cmd_resummarize(
    date: str, hora: str | None, retranscribe: bool = False, email: bool = True
) -> int:
    cfg = load_config()

    from wendes.distribution.email import send_ata
    from wendes.history.store import HistoryStore
    from wendes.llm.client import Llm
    from wendes.minutes.summarizer import generate_ata, generate_stealth_ata
    from wendes.speech.stt_batch import (
        load_transcript,
        save_transcript,
        transcribe_recording,
    )

    store = HistoryStore(cfg)
    record = store.load_record(date, hora)
    if record is None:
        runs = store.list_runs(date)
        print(f"Nenhuma reunião registrada em {date} {hora or ''}".strip())
        if runs:
            print(f"Reuniões desse dia: {', '.join(runs)} (use --hora HH:MM)")
        return 1
    run_dir = store.run_dir(date, record.started_at)
    wav = run_dir / "recording.wav"

    transcript = None if retranscribe else load_transcript(run_dir, cfg.stt.corrections)
    if transcript is None and wav.exists():
        print(f"Retranscrevendo {wav.name} com o modelo {cfg.stt.batch_model}…")
        transcript = transcribe_recording(cfg, wav)
    if transcript:
        # Persiste também correções novas aplicadas a uma transcrição já salva.
        save_transcript(run_dir, transcript, cfg.stt.corrections)

    try:
        llm = Llm(cfg)
    except Exception:  # noqa: BLE001
        llm = None
    if record.mode == "assist":
        from wendes.assistant.finalize import finalize_assist

        path = finalize_assist(
            cfg, store, run_dir, record, wav, llm=llm, distribute=email,
        )
        print(f"Ata regenerada: {path}")
        return 0
    if record.mode:
        stealth = record.mode == "stealth"
    else:
        # Registro antigo (sem mode): stealth não tem respostas estruturadas.
        stealth = not any(p.answers for p in record.people)
    generate = generate_stealth_ata if stealth else generate_ata
    ata = generate(cfg, llm, record, transcript)
    path = store.save(record, ata)
    print(f"Ata regenerada: {path}")
    if email:
        send_ata(cfg, ata, date)
    return 0


def cmd_serve(host: str | None, port: int | None) -> int:
    cfg = load_config()
    token = cfg.secrets.wendes_web_token
    if not token:
        print("WENDES_WEB_TOKEN não configurado no .env — o serve não sobe sem token.")
        return 1

    from wendes.assistant.web.archive import ArchiveServer, make_ask
    from wendes.history.store import HistoryStore
    from wendes.llm.client import Llm

    HistoryStore(cfg).db.close()  # garante o schema
    server = ArchiveServer(cfg.resolve(cfg.data_dir), token, ask=make_ask(cfg, Llm(cfg)))
    host = host or cfg.assistant.web.host
    port = port or cfg.assistant.web.port + 1
    print(f"wendes serve em http://{host}:{port}/?token=<WENDES_WEB_TOKEN>")
    try:
        asyncio.run(server.run(host, port))
    except KeyboardInterrupt:
        pass
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="wendes")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="verifica o ambiente")
    p_join = sub.add_parser("join-test", help="entra na sala, fala e loga o roster")
    p_join.add_argument("--stay", type=int, default=60, help="segundos na sala")
    sub.add_parser("run", help="conduz a daily completa")
    p_st = sub.add_parser(
        "stealth", help="entra mudo, só grava/transcreve; /fim no chat encerra e envia a ata"
    )
    p_st.add_argument(
        "--max-hours", type=float, default=4.0,
        help="limite de segurança da gravação se ninguém enviar /fim (default: 4h)",
    )
    p_st.add_argument(
        "--url", default=None,
        help="URL da sala a usar no lugar de room.url do config.yaml "
        "(moderated.jitsi.net ou meet.jit.si)",
    )
    p_as = sub.add_parser(
        "assist", help="assistente visível: transcreve, notas, perguntas e artefatos ao vivo"
    )
    p_as.add_argument("--url", default=None, help="URL da sala (default: room.url)")
    p_as.add_argument(
        "--max-hours", type=float, default=4.0,
        help="limite de segurança se ninguém enviar /fim (default: 4h)",
    )
    p_as.add_argument(
        "--replay", metavar="AAAA-MM-DD", default=None,
        help="roda o pipeline sobre o recording.wav dessa data, sem Jitsi",
    )
    p_as.add_argument("--hora", default=None, help="HH:MM da reunião do replay")
    p_as.add_argument(
        "--speed", type=float, default=1.0,
        help="ritmo do replay (1 = tempo real, 4 = 4x, 0 = o mais rápido possível)",
    )
    p_ask = sub.add_parser("ask", help="pergunta sobre reuniões passadas")
    p_ask.add_argument("question", help="a pergunta, entre aspas")
    p_ask.add_argument("--date", default=None, help="AAAA-MM-DD de uma reunião específica")
    p_ask.add_argument("--hora", default=None, help="HH:MM (com --date)")
    sub.add_parser("reindex", help="indexa o histórico para a busca das perguntas")
    p_sv = sub.add_parser("serve", help="servidor do histórico: reuniões, busca, perguntas")
    p_sv.add_argument("--host", default=None, help="default: assistant.web.host")
    p_sv.add_argument("--port", type=int, default=None, help="default: assistant.web.port + 1")
    p_sa = sub.add_parser("send-actions", help="reenvia o webhook de ações de uma reunião")
    p_sa.add_argument("date", help="AAAA-MM-DD")
    p_sa.add_argument("--hora", default=None, help="HH:MM (default: a última do dia)")
    p_re = sub.add_parser("resummarize", help="regera a ata de uma data")
    p_re.add_argument("date", help="data no formato YYYY-MM-DD")
    p_re.add_argument(
        "--hora", default=None,
        help="HH:MM de uma reunião específica (default: a última do dia)",
    )
    p_re.add_argument(
        "--retranscribe", action="store_true",
        help="retranscreve o áudio mesmo se houver transcript.json salvo",
    )
    p_re.add_argument(
        "--no-email", action="store_true", help="não envia a ata regenerada por e-mail"
    )
    args = parser.parse_args()

    if args.command == "check":
        sys.exit(cmd_check())
    if args.command == "join-test":
        sys.exit(asyncio.run(_join_test(args.stay)))
    if args.command == "run":
        sys.exit(asyncio.run(_run()))
    if args.command == "stealth":
        sys.exit(asyncio.run(_stealth(args.max_hours, room_url=args.url)))
    if args.command == "assist":
        if args.replay:
            sys.exit(asyncio.run(_assist_replay(args.replay, args.hora, args.speed)))
        sys.exit(asyncio.run(_assist(args.max_hours, room_url=args.url)))
    if args.command == "ask":
        sys.exit(cmd_ask(args.question, args.date, args.hora))
    if args.command == "send-actions":
        sys.exit(cmd_send_actions(args.date, args.hora))
    if args.command == "reindex":
        sys.exit(cmd_reindex())
    if args.command == "serve":
        sys.exit(cmd_serve(args.host, args.port))
    if args.command == "resummarize":
        sys.exit(
            cmd_resummarize(
                args.date, args.hora,
                retranscribe=args.retranscribe, email=not args.no_email,
            )
        )


if __name__ == "__main__":
    main()
