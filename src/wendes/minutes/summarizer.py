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

ATA_MAX_TOKENS = 3000
ATA_TIMEOUT_S = 180.0
# Acima disso a transcrição não vai inteira ao LLM: condensa por blocos antes.
SINGLE_PASS_CHARS = 45000
CHUNK_CHARS = 30000

_FORMAT_INSTRUCTIONS = (
    "Responda em duas seções de markdown, exatamente neste formato:\n"
    "RESUMO:\n{resumo}\n"
    "ACOES:\n<lista markdown de impedimentos e ações;"
    " se não houver, escreva 'Nenhum impedimento relatado.'>\n"
    "FIM\n"
    "(termine a resposta com a linha FIM)"
)

_NO_ATTRIBUTION = (
    "A transcrição NÃO identifica quem está falando. NUNCA atribua uma fala,"
    " tarefa ou impedimento a uma pessoa por dedução — cite um nome apenas se"
    " ele aparecer explicitamente no conteúdo da própria fala (ex.: 'o Fulano"
    " vai revisar o PR'). Na dúvida, escreva de forma impessoal.\n"
)


def _glossary(cfg: Config) -> str:
    terms: list[str] = []
    if cfg.stt.vocabulary:
        terms.append(cfg.stt.vocabulary)
    terms.extend(m.name for m in cfg.team.members)
    return ", ".join(terms)


def _glossary_instructions(cfg: Config) -> str:
    return (
        "A transcrição é automática e contém erros fonéticos de reconhecimento"
        " de voz. Corrija termos que se aproximem do glossário a seguir e NÃO"
        " reproduza palavras inexistentes em português — se um trecho for"
        " ininteligível, omita-o ou marque [inaudível].\n"
        f"Glossário (nomes e jargões da equipe): {_glossary(cfg)}.\n"
    )


def _transcript_text(transcript: list[TranscriptSegment] | None) -> str:
    return "\n".join(s.text for s in (transcript or []))


def _split_chunks(text: str, chunk_chars: int = CHUNK_CHARS) -> list[str]:
    """Divide em blocos de até `chunk_chars`, respeitando quebras de linha."""
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.splitlines():
        if size + len(line) > chunk_chars and current:
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


def _condense(cfg: Config, llm: Llm, text: str) -> str:
    """Map: transforma cada bloco da transcrição em notas por pessoa."""
    chunks = _split_chunks(text)
    log.info("Transcrição longa (%d chars) — condensando em %d blocos", len(text), len(chunks))
    notes: list[str] = []
    for i, chunk in enumerate(chunks, 1):
        resp = llm.ask_or_none(
            "Você recebeu um trecho da transcrição automática de uma reunião"
            f" daily (parte {i} de {len(chunks)}).\n"
            + _glossary_instructions(cfg)
            + _NO_ATTRIBUTION
            + f"\nTrecho:\n{chunk}\n\n"
            "Resuma em notas de markdown, por assunto: o que foi relatado"
            " (feito, planejado), decisões tomadas e impedimentos/ações."
            " Só inclua o que está no trecho.",
            max_tokens=1500,
            timeout=ATA_TIMEOUT_S,
            continue_on_length=True,
        )
        if resp:
            notes.append(resp)
    return "\n\n".join(notes)


def _ask_ata(llm: Llm, prompt: str) -> tuple[str, str] | None:
    """Chama o LLM e extrai (resumo, ações); None se falhar ou vier fora do formato."""
    resp = llm.ask_or_none(
        prompt, max_tokens=ATA_MAX_TOKENS, timeout=ATA_TIMEOUT_S, continue_on_length=True
    )
    if not resp or "ACOES:" not in resp:
        return None
    summary = resp.split("RESUMO:")[-1].split("ACOES:")[0].strip()
    actions = resp.split("ACOES:")[-1].strip()
    if actions.rstrip().endswith("FIM"):
        actions = actions.rstrip()[: -len("FIM")].rstrip()
    else:
        log.warning("Ata sem a sentinela FIM — resposta pode ter sido cortada")
    return summary, actions


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
        full_text = _transcript_text(transcript)
        if len(full_text) > SINGLE_PASS_CHARS:
            full_text = _condense(cfg, llm, full_text)
        answers = json.dumps(
            [asdict(p) for p in record.people], ensure_ascii=False, indent=1
        )
        parsed = _ask_ata(
            llm,
            "Gere a ata da daily de hoje. Respostas estruturadas coletadas:\n"
            f"{answers}\n\n"
            + _glossary_instructions(cfg)
            + "As respostas estruturadas acima têm autor confiável; já para a"
            " transcrição vale o seguinte: "
            + _NO_ATTRIBUTION
            + f"\nTranscrição completa da reunião:\n{full_text}\n\n"
            + _FORMAT_INSTRUCTIONS.format(resumo="<parágrafo curto do andamento geral>"),
        )
        if parsed:
            summary, actions = parsed

    template = _env.get_template("ata.md.j2")
    return template.render(
        date=record.date,
        started_at=record.started_at,
        room_url=cfg.room.url,
        interrupted=record.interrupted,
        error=record.error,
        people=record.people,
        summary=summary,
        action_items=actions,
    )


def generate_stealth_ata(
    cfg: Config,
    llm: Llm | None,
    record: MeetingRecord,
    transcript: list[TranscriptSegment] | None,
    room_url: str | None = None,
) -> str:
    """Ata do modo stealth: só há gravação/transcrição, sem respostas estruturadas."""
    attendees = [p.name for p in record.people if p.present]
    full_text = _transcript_text(transcript)
    summary = f"Participaram: {', '.join(attendees) or 'ninguém identificado'}."
    actions = "Nenhum impedimento identificado na transcrição."
    if not full_text.strip():
        summary += " A gravação não produziu transcrição — verifique o áudio."
    elif llm is not None:
        if len(full_text) > SINGLE_PASS_CHARS:
            full_text = _condense(cfg, llm, full_text)
        parsed = _ask_ata(
            llm,
            "Você recebeu a transcrição automática de uma reunião daily (pode conter"
            " erros de reconhecimento de voz). Participantes na sala: "
            f"{', '.join(attendees) or 'desconhecidos'}.\n"
            + _glossary_instructions(cfg)
            + _NO_ATTRIBUTION
            + f"\nTranscrição:\n{full_text}\n\n"
            "Gere a ata da reunião. "
            + _FORMAT_INSTRUCTIONS.format(
                resumo="<parágrafos curtos, organizados por assunto, com o que"
                " foi relatado (ontem, hoje)>"
            ),
        )
        if parsed:
            summary, actions = parsed

    template = _env.get_template("ata_stealth.md.j2")
    return template.render(
        date=record.date,
        started_at=record.started_at,
        room_url=room_url or cfg.room.url,
        interrupted=record.interrupted,
        error=record.error,
        attendees=attendees,
        summary=summary,
        action_items=actions,
    )
