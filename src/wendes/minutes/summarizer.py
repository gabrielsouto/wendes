"""Gera a ata a partir do registro da reunião + retranscrição, via gemma."""
from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from wendes.config import Config
from wendes.facilitator.state_machine import MeetingRecord
from wendes.llm.client import Llm
from wendes.speech.stt_batch import TranscriptSegment

log = logging.getLogger(__name__)

_env = Environment(loader=FileSystemLoader(Path(__file__).parent / "templates"))


def _fallback_summary(record: MeetingRecord) -> tuple[str, str]:
    presentes = [p.name for p in record.people if p.present]
    bloqueios = [p.name for p in record.people if p.has_blocker]
    summary = f"Participaram: {', '.join(presentes) or 'ninguém identificado'}."
    actions = (
        "Impedimentos relatados por: " + ", ".join(bloqueios)
        if bloqueios
        else "Nenhum impedimento relatado."
    )
    return summary, actions


def generate_ata(
    cfg: Config,
    llm: Llm | None,
    record: MeetingRecord,
    transcript: list[TranscriptSegment] | None,
) -> str:
    summary, actions = _fallback_summary(record)
    if llm is not None:
        full_text = "\n".join(s.text for s in (transcript or []))
        answers = json.dumps(
            [asdict(p) for p in record.people], ensure_ascii=False, indent=1
        )
        resp = llm.ask_or_none(
            "Gere a ata da daily de hoje. Respostas estruturadas coletadas:\n"
            f"{answers}\n\nTranscrição completa da reunião (pode conter erros):\n"
            f"{full_text[:60000]}\n\n"
            "Responda em duas seções de markdown, exatamente neste formato:\n"
            "RESUMO:\n<parágrafo curto do andamento geral>\n"
            "ACOES:\n<lista markdown de impedimentos e ações, cada um com responsável;"
            " se não houver, escreva 'Nenhum impedimento relatado.'>",
            max_tokens=800,
        )
        if resp and "ACOES:" in resp:
            summary = resp.split("RESUMO:")[-1].split("ACOES:")[0].strip()
            actions = resp.split("ACOES:")[-1].strip()

    template = _env.get_template("ata.md.j2")
    return template.render(
        date=record.date,
        started_at=record.started_at,
        room_url=cfg.room.url,
        people=record.people,
        summary=summary,
        action_items=actions,
    )


def generate_stealth_ata(
    cfg: Config,
    llm: Llm | None,
    record: MeetingRecord,
    transcript: list[TranscriptSegment] | None,
) -> str:
    """Ata do modo stealth: só há gravação/transcrição, sem respostas estruturadas."""
    attendees = [p.name for p in record.people if p.present]
    full_text = "\n".join(s.text for s in (transcript or []))
    summary = f"Participaram: {', '.join(attendees) or 'ninguém identificado'}."
    actions = "Nenhum impedimento identificado na transcrição."
    if not full_text.strip():
        summary += " A gravação não produziu transcrição — verifique o áudio."
    elif llm is not None:
        resp = llm.ask_or_none(
            "Você recebeu a transcrição automática de uma reunião daily (pode conter"
            f" erros de reconhecimento de voz). Participantes na sala: "
            f"{', '.join(attendees) or 'desconhecidos'}.\n\n"
            f"Transcrição:\n{full_text[:60000]}\n\n"
            "Gere a ata da reunião. Responda em duas seções de markdown, exatamente"
            " neste formato:\n"
            "RESUMO:\n<parágrafos curtos com o que cada pessoa relatou (ontem, hoje),"
            " atribuindo às pessoas quando possível>\n"
            "ACOES:\n<lista markdown de impedimentos e ações, cada um com responsável;"
            " se não houver, escreva 'Nenhum impedimento relatado.'>",
            max_tokens=1000,
        )
        if resp and "ACOES:" in resp:
            summary = resp.split("RESUMO:")[-1].split("ACOES:")[0].strip()
            actions = resp.split("ACOES:")[-1].strip()

    template = _env.get_template("ata_stealth.md.j2")
    return template.render(
        date=record.date,
        started_at=record.started_at,
        room_url=cfg.room.url,
        attendees=attendees,
        summary=summary,
        action_items=actions,
    )
