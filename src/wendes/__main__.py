"""CLI do wendes.

Comandos:
  check        verifica ambiente (áudio, TTS, LLM, token Firebase)
  join-test    Fase 1: entra na sala, fala uma frase e loga o roster
  run          conduz a daily completa (facilita, transcreve, ata, distribui)
  stealth      entra mudo e só grava/transcreve; /fim no chat encerra e envia a ata
  resummarize  regera a ata de uma data a partir do histórico, sem reentrar na sala
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
        print(f"✔ vLLM ok ({resp[:40]!r})")
    except Exception as exc:  # noqa: BLE001
        print(f"✘ vLLM: {exc}")
        ok = False

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

    browser = JitsiBrowser()
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

        ChatCommands(cfg, room)  # permite testar /ajuda, /comece etc. no chat
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


async def _run() -> int:
    cfg = load_config()

    from wendes.audio.capture import AudioCapture
    from wendes.audio.devices import ensure_devices
    from wendes.distribution.email import send_ata
    from wendes.distribution.webhook import post_ata
    from wendes.facilitator.state_machine import Facilitator
    from wendes.history.store import HistoryStore
    from wendes.jitsi.browser import JitsiBrowser
    from wendes.jitsi.entry import EntryError, enter_room
    from wendes.jitsi.room import JitsiRoom
    from wendes.llm.client import Llm
    from wendes.minutes.summarizer import generate_ata
    from wendes.speech.stt_batch import save_transcript, transcribe_recording
    from wendes.speech.stt_live import LiveStt
    from wendes.speech.tts import Tts

    ensure_devices()
    store = HistoryStore(cfg)
    tts = Tts(cfg)
    stt = LiveStt(cfg)
    try:
        llm = Llm(cfg)
    except Exception:  # noqa: BLE001
        llm = None

    from datetime import datetime

    now = datetime.now()
    date = now.strftime("%Y-%m-%d")
    started_at = now.strftime("%H:%M")
    run_dir = store.run_dir(date, started_at)
    wav_path = run_dir / "recording.wav"

    file_handler = logging.FileHandler(run_dir / "run.log", encoding="utf-8")
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    logging.getLogger().addHandler(file_handler)

    browser = JitsiBrowser()
    capture = AudioCapture(wav_path)
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
        await room.install_event_bridge()
        await room.unmute()
        await capture.start()

        from wendes.facilitator.commands import ChatCommands

        commands = ChatCommands(cfg, room)
        facilitator = Facilitator(cfg, room, capture, tts, stt, llm, commands)
        facilitator.record.date = date
        facilitator.record.started_at = started_at  # alinha com a pasta da execução
        record = facilitator.record  # mantém referência mesmo se a reunião falhar no meio
        record = await facilitator.run()
        await room.hangup()
    finally:
        await capture.stop()
        await browser.close()

    log.info("Reunião encerrada — gerando ata (retranscrição com modelo %s)", cfg.stt.batch_model)
    transcript = transcribe_recording(cfg, wav_path) if wav_path.exists() else None
    if transcript:
        save_transcript(run_dir, transcript, cfg.stt.corrections)
    ata = generate_ata(cfg, llm, record, transcript)
    ata_path = store.save(record, ata)
    log.info("Ata salva em %s", ata_path)
    send_ata(cfg, ata, record.date)
    post_ata(cfg, ata)
    return 0


async def _stealth(max_hours: float, room_url: str | None = None) -> int:
    """Modo stealth: entra mudo, grava e transcreve tudo; /fim encerra e envia a ata."""
    cfg = load_config()

    from datetime import datetime

    from wendes.audio.capture import AudioCapture
    from wendes.audio.devices import ensure_devices
    from wendes.distribution.email import send_ata
    from wendes.distribution.webhook import post_ata
    from wendes.facilitator.state_machine import MeetingRecord, PersonRecord
    from wendes.facilitator.stealth import StealthSession, canonical_name, is_bot_display
    from wendes.history.store import HistoryStore
    from wendes.jitsi.browser import JitsiBrowser
    from wendes.jitsi.entry import EntryError, enter_room
    from wendes.jitsi.room import JitsiRoom
    from wendes.llm.client import Llm
    from wendes.minutes.summarizer import generate_stealth_ata
    from wendes.speech.stt_batch import save_transcript, transcribe_recording

    ensure_devices()
    store = HistoryStore(cfg)

    now = datetime.now()
    date = now.strftime("%Y-%m-%d")
    started_at = now.strftime("%H:%M")
    run_dir = store.run_dir(date, started_at)
    wav_path = run_dir / "recording.wav"

    file_handler = logging.FileHandler(run_dir / "run.log", encoding="utf-8")
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    logging.getLogger().addHandler(file_handler)

    browser = JitsiBrowser()
    capture = AudioCapture(wav_path)
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
        await room.install_event_bridge()
        await room.mute()  # sem voz; única mensagem de chat é o aviso de gravação
        await room.send_chat(
            "🔴 Gravando em modo escuta — não vou interagir. "
            "Ao final, a ata será gerada e enviada por e-mail. Envie /fim para encerrar."
        )

        session = StealthSession(cfg, room)
        await capture.start()
        log.info("Gravando em modo stealth — /fim no chat encerra (limite %.1fh)", max_hours)
        await session.wait_finished(max_hours=max_hours)
        await room.hangup()
    finally:
        await capture.stop()
        await browser.close()

    # attendees → nomes canônicos do roster quando casarem (sem contar o bot)
    record = MeetingRecord(date=date, started_at=started_at)
    seen: set[str] = set()
    for display in session.attendees.values():
        if is_bot_display(cfg, display):
            continue
        name = canonical_name(cfg, display)
        if name not in seen:
            seen.add(name)
            record.people.append(PersonRecord(name=name, present=True))

    log.info("Gravação encerrada — transcrevendo com o modelo %s", cfg.stt.batch_model)
    transcript = transcribe_recording(cfg, wav_path) if wav_path.exists() else None
    if transcript:
        save_transcript(run_dir, transcript, cfg.stt.corrections)
    try:
        llm = Llm(cfg)
    except Exception:  # noqa: BLE001
        llm = None
    ata = generate_stealth_ata(cfg, llm, record, transcript, room_url=room_url)
    ata_path = store.save(record, ata)
    log.info("Ata salva em %s", ata_path)
    send_ata(cfg, ata, date)
    post_ata(cfg, ata)
    return 0


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
    # Reunião stealth não tem respostas estruturadas — usa a ata de transcrição.
    stealth = not any(p.answers for p in record.people)
    generate = generate_stealth_ata if stealth else generate_ata
    ata = generate(cfg, llm, record, transcript)
    path = store.save(record, ata)
    print(f"Ata regenerada: {path}")
    if email:
        send_ata(cfg, ata, date)
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
    if args.command == "resummarize":
        sys.exit(
            cmd_resummarize(
                args.date, args.hora,
                retranscribe=args.retranscribe, email=not args.no_email,
            )
        )


if __name__ == "__main__":
    main()
