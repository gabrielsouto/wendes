"""Máquina de estados da daily.

WAITING_QUORUM → OPENING → ROLL_CALL → PERSON_ROUND(i) [ASK → LISTEN →
FOLLOWUP?] → CLOSING. Determinística; o LLM só refina follow-ups.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime

from rapidfuzz import fuzz

from wendes.audio.capture import AudioCapture
from wendes.audio.playback import play_wav
from wendes.config import Config, Member
from wendes.facilitator import prompts_ptbr as P
from wendes.facilitator.analysis import analyze_blocker_answer
from wendes.facilitator.commands import ChatCommands
from wendes.jitsi.room import JitsiRoom
from wendes.llm.client import Llm
from wendes.speech.stt_live import LiveStt, Utterance
from wendes.speech.tts import Tts

log = logging.getLogger(__name__)


class MeetingEnded(Exception):
    """Disparada pelo /encerrar para pular direto ao fechamento."""


_SKIPPED = object()  # sentinela: /pular durante a escuta


@dataclass
class Answer:
    question: str
    text: str


@dataclass
class PersonRecord:
    name: str
    present: bool = False
    answers: list[Answer] = field(default_factory=list)
    has_blocker: bool = False


@dataclass
class MeetingRecord:
    date: str
    started_at: str
    people: list[PersonRecord] = field(default_factory=list)
    raw_utterances: list[dict] = field(default_factory=list)


class Facilitator:
    def __init__(
        self,
        cfg: Config,
        room: JitsiRoom,
        capture: AudioCapture,
        tts: Tts,
        stt: LiveStt,
        llm: Llm | None,
        commands: ChatCommands | None = None,
    ) -> None:
        self.cfg = cfg
        self.room = room
        self.capture = capture
        # Fila própria de áudio: só existe enquanto há quem escute (modo run).
        self.audio = capture.subscribe()
        self.tts = tts
        self.stt = stt
        self.llm = llm
        self.commands = commands
        now = datetime.now()
        self.record = MeetingRecord(
            date=now.strftime("%Y-%m-%d"), started_at=now.strftime("%H:%M")
        )

    async def say(self, text: str, chat: bool = False) -> None:
        if chat:
            try:
                await self.room.send_chat(text)
            except Exception:  # noqa: BLE001 — chat é acessório, voz é o canal principal
                pass
        wav = self.tts.synthesize(P.spoken(text, self.cfg.tts.phonetic))
        await play_wav(str(wav))

    def _match_roster(self, display_name: str) -> Member | None:
        for member in self.cfg.team.members:
            for candidate in (member.name, *member.aliases):
                if fuzz.partial_ratio(candidate.lower(), display_name.lower()) >= 85:
                    return member
        return None

    async def _build_round(self, already_done: set[str]) -> tuple[list[Member], list[Member]]:
        """Monta a rodada a partir de quem ESTÁ na sala (lista dinâmica).

        Quem casa com o roster do config ganha nome canônico; visitantes de
        outras equipes entram pelo display name do Jitsi. Retorna também os
        membros do roster ausentes.
        """
        participants = await self.room.list_participants()
        display_names = [p.display_name for p in participants if p.display_name.strip()]
        round_list: list[Member] = []
        matched: set[str] = set()
        for display in display_names:
            member = self._match_roster(display)
            if member is not None:
                if member.name not in matched:
                    matched.add(member.name)
                    if member.name not in already_done:
                        round_list.append(member)
            elif display not in already_done:
                round_list.append(Member(name=display))
        absent = [m for m in self.cfg.team.members if m.name not in matched]
        return round_list, absent

    async def wait_quorum(self) -> bool:
        deadline = asyncio.get_event_loop().time() + self.cfg.meeting.wait_quorum_minutes * 60
        while asyncio.get_event_loop().time() < deadline:
            participants = await self.room.list_participants()
            if len(participants) >= self.cfg.meeting.quorum:
                return True
            await asyncio.sleep(10)
        return False

    async def _wait_start_command(self) -> bool:
        assert self.commands is not None
        await self.say(P.WAIT_COMMAND)
        await self.room.send_chat(
            "Aguardando o comando /comece para iniciar a rodada (/ajuda lista os comandos)."
        )
        timeout = self.cfg.meeting.wait_quorum_minutes * 60
        try:
            await asyncio.wait_for(self.commands.start.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def _ack(self) -> None:
        """Feedback imediato ao fim da fala, mascarando o tempo de transcrição."""
        await self.say(P.ACK)

    async def _listen(self, max_seconds: float) -> Utterance | None | object:
        """Escuta, mas /pular e /encerrar interrompem imediatamente."""
        listen = asyncio.create_task(
            self.stt.listen_utterance(
                self.audio, max_seconds=max_seconds, on_speech_end=self._ack
            )
        )
        if self.commands is None:
            return await listen
        skip = asyncio.create_task(self.commands.skip.wait())
        end = asyncio.create_task(self.commands.end.wait())
        done, pending = await asyncio.wait(
            {listen, skip, end}, return_when=asyncio.FIRST_COMPLETED
        )
        for t in pending:
            t.cancel()
        if end in done:
            raise MeetingEnded()
        if skip in done:
            self.commands.skip.clear()
            return _SKIPPED
        return listen.result()

    def _check_end(self) -> None:
        if self.commands is not None and self.commands.end.is_set():
            raise MeetingEnded()

    async def run(self) -> MeetingRecord:
        if self.commands is not None and self.cfg.commands.start_on_command:
            if not await self._wait_start_command():
                log.error("Ninguém enviou /comece dentro do tempo limite — encerrando")
                return self.record
        elif not await self.wait_quorum():
            log.error("Quórum não atingido — encerrando")
            return self.record

        absent: list[Member] = []
        try:
            await self.say(P.OPENING)

            done: set[str] = set()
            round_list, absent = await self._build_round(done)
            if round_list:
                await self.say(
                    P.ROLL_CALL.format(names=", ".join(m.name for m in round_list))
                )
            if absent:
                await self.say(P.ABSENT.format(names=", ".join(m.name for m in absent)))

            for member in round_list:
                self._check_end()
                await self._person_round(member)
                done.add(member.name)

            # atrasados: quem entrou depois da chamada ganha a vez antes de encerrar
            late, absent = await self._build_round(done)
            for member in late:
                self._check_end()
                await self._person_round(member)
                done.add(member.name)
        except MeetingEnded:
            log.info("Reunião encerrada por comando /encerrar")

        for m in absent:
            self.record.people.append(PersonRecord(name=m.name, present=False))

        await self.say(P.CLOSING)
        return self.record

    async def _person_round(self, member: Member) -> PersonRecord:
        rec = PersonRecord(name=member.name, present=True)
        # registra já no início: um /encerrar no meio da vez preserva o parcial
        self.record.people.append(rec)
        timebox = self.cfg.meeting.timebox_per_person_seconds
        per_question = max(60, timebox // max(1, len(self.cfg.meeting.questions)))

        for i, question in enumerate(self.cfg.meeting.questions):
            prompt = (
                P.ASK_PERSON.format(name=member.name, question=question)
                if i == 0
                else P.NEXT_QUESTION.format(question=question)
            )
            await self.say(prompt, chat=True)
            utt = await self._listen(max_seconds=per_question)
            if utt is _SKIPPED:
                await self.say(P.SKIPPED)
                return rec
            if utt is None:
                if i == 0:
                    await self.say(P.NO_ANSWER.format(name=member.name))
                    return rec
                continue
            rec.answers.append(Answer(question=question, text=utt.text))
            self.record.raw_utterances.append(
                {"speaker": member.name, "question": question, "text": utt.text}
            )
            if utt.duration_s >= per_question:
                await self.say(P.TIMEBOX_WARNING.format(name=member.name))
                extra = await self._listen(max_seconds=20)
                if extra is _SKIPPED:
                    await self.say(P.SKIPPED)
                    return rec
                if extra:
                    rec.answers[-1].text += " " + extra.text

            if "impedimento" in question.lower():
                analysis = analyze_blocker_answer(self.llm, member.name, utt.text)
                rec.has_blocker = analysis.has_blocker
                if analysis.followup:
                    await self.say(
                        P.FOLLOWUP_BLOCKER.format(name=member.name, followup=analysis.followup)
                    )
                    detail = await self._listen(max_seconds=45)
                    if detail is _SKIPPED:
                        await self.say(P.SKIPPED)
                        return rec
                    if detail:
                        rec.answers.append(
                            Answer(question=analysis.followup, text=detail.text)
                        )

        await self.say(P.THANKS_NEXT.format(name=member.name))
        return rec
