#!/usr/bin/env python3
"""Fase 0 do modo assistente: coleta dados para medir a atribuição de falas.

Entra na sala como o modo stealth (mudo, com aviso de gravação no chat) e, por
N minutos (default 10), grava o WAV e registra todo evento de nível de áudio e
de dominant speaker com `t_audio` = amostras gravadas / 16000 (SEM offset; o
eval_speakers.py testa os offsets depois). No terminal, a cada 10 s, mostra
quantos eventos por segundo chegaram de cada participante.

Saída em data/spike/AAAA-MM-DD_HH-MM/:
  recording.wav        gravação
  audio_levels.jsonl   {"t","pid","level"} · {"t","pid","dominant":true} · {"pid","name"}
  diagnostics.json     nomes de JitsiMeetJS.events.conference, ouvintes, sendPrivateTextMessage
  participants.json    pid → nome visto durante a coleta

Uso (sob Xvfb, com o áudio virtual configurado):
  scripts/setup_audio.sh
  xvfb-run -a .venv/bin/python scripts/spike_speakers.py --minutes 10 [--url URL]

Depois, rotule no Audacity quem falou quando (faixa de rótulos → Exportar
rótulos → labels.txt na mesma pasta) e rode scripts/eval_speakers.py.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from collections import Counter
from datetime import datetime

from wendes.assistant.speakers import SpeakerTracker
from wendes.config import load_config

log = logging.getLogger("spike")


async def main(minutes: float, url: str | None) -> int:
    cfg = load_config()

    from wendes.audio.capture import SAMPLE_RATE, AudioCapture
    from wendes.audio.devices import ensure_devices
    from wendes.jitsi.browser import JitsiBrowser
    from wendes.jitsi.entry import EntryError, enter_room
    from wendes.jitsi.room import JitsiRoom

    ensure_devices()
    out_dir = cfg.resolve(cfg.data_dir) / "spike" / datetime.now().strftime("%Y-%m-%d_%H-%M")
    out_dir.mkdir(parents=True, exist_ok=True)
    capture = AudioCapture(out_dir / "recording.wav")
    tracker = SpeakerTracker(
        lambda: capture.samples_written / SAMPLE_RATE,
        latency_offset_s=0.0,
        log_path=out_dir / "audio_levels.jsonl",
    )
    counts: Counter[str] = Counter()
    names: dict[str, str] = {}

    def on_levels(items):
        tracker.on_levels(items)
        for pid, _ in items:
            counts[pid] += 1

    browser = JitsiBrowser(cfg)
    try:
        page = await browser.start()
        try:
            await enter_room(page, cfg, cfg.secrets.firebase_refresh_token, room_url=url)
        except EntryError as exc:
            log.error("%s", exc)
            return 1
        room = JitsiRoom(page)
        await room.wait_joined(cfg.room.join_timeout_seconds)
        await room.install_event_bridge()
        room.on_dominant_speaker(tracker.on_dominant)
        room.on_audio_levels(on_levels)

        def on_participant(kind, pid, name):
            if name:
                names[pid] = name
                tracker.set_name(pid, name)

        room.on_participant(on_participant)
        diag = await room.install_assist_bridge(levels=True)
        diag["sendPrivateTextMessage"] = room.can_send_private
        (out_dir / "diagnostics.json").write_text(
            json.dumps(diag, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        await room.mute()
        await room.send_chat(
            "🔴 Gravando um teste de identificação de falantes (10 min). Não vou interagir."
        )
        await capture.start()
        log.info("Coletando por %.1f min em %s", minutes, out_dir)
        deadline = time.monotonic() + minutes * 60
        last = time.monotonic()
        while time.monotonic() < deadline:
            await asyncio.sleep(10)
            for p in await room.list_participants():
                if p.display_name:
                    names[p.id] = p.display_name
                    tracker.set_name(p.id, p.display_name)
            now = time.monotonic()
            span = max(now - last, 1e-6)
            rates = {names.get(pid, pid): round(n / span, 1) for pid, n in counts.items()}
            print(
                f"[{capture.samples_written / SAMPLE_RATE:7.1f}s] eventos/s por participante:"
                f" {rates or '(nenhum)'}",
                flush=True,
            )
            counts.clear()
            last = now
        await room.hangup()
    finally:
        tracker.close()
        await capture.stop()
        await browser.close()
    (out_dir / "participants.json").write_text(
        json.dumps(names, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"Pronto: {out_dir}")
    print("Rotule no Audacity, exporte labels.txt nessa pasta e rode scripts/eval_speakers.py")
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--minutes", type=float, default=10.0)
    ap.add_argument("--url", default=None, help="sala (default: room.url do config.yaml)")
    args = ap.parse_args()
    raise SystemExit(asyncio.run(main(args.minutes, args.url)))
