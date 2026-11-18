"""Gatilhos do modo assist: comandos do chat e palavra de ativação por voz."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from rapidfuzz import fuzz

ARTIFACT_COMMANDS = {
    "/doc": "doc",
    "/documento": "doc",
    "/diagrama": "diagrama",
    "/cronograma": "cronograma",
    "/checklist": "checklist",
    "/tabela": "tabela",
}
ALIASES = {
    "/ações": "/acoes",
    "/acões": "/acoes",
    "/pendências": "/pendencias",
    "/pausa": "/pausar",
    "/retoma": "/retomar",
    "/help": "/ajuda",
    "/encerrar": "/fim",
}
SIMPLE_COMMANDS = {
    "/auth", "/resumo", "/acoes", "/pendencias", "/cobrar", "/pausar", "/retomar",
    "/link", "/tela", "/fim", "/ajuda",
}


def _norm(text: str) -> str:
    dec = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in dec if not unicodedata.combining(c))


@dataclass
class Command:
    name: str  # "ask", "artifact", ou o comando sem barra ("resumo", "pausar"…)
    arg: str = ""
    kind: str = ""  # tipo do artefato, para name == "artifact"
    ref: int | None = None  # "#2" no início do pedido de artefato


_REF = re.compile(r"^#(\d+)\s*")


def parse_chat(text: str, bot_name: str, private: bool = False) -> Command | None:
    """Interpreta uma mensagem do chat; None = não é para o bot."""
    text = text.strip()
    if not text:
        return None
    bot = _norm(bot_name).strip()
    if text.startswith("/"):
        head, _, arg = text.partition(" ")
        head = ALIASES.get(head.lower(), head.lower())
        arg = arg.strip()
        if _norm(head[1:]) in ("wendes", bot):
            return Command("ask", arg)
        if head in ARTIFACT_COMMANDS:
            ref = None
            m = _REF.match(arg)
            if m:
                ref = int(m.group(1))
                arg = arg[m.end():].strip()
            return Command("artifact", arg, kind=ARTIFACT_COMMANDS[head], ref=ref)
        if head in SIMPLE_COMMANDS:
            return Command(head[1:], arg)
        return Command("desconhecido", head)
    if text.startswith("@"):
        first, _, rest = text[1:].partition(" ")
        target = _norm(first).strip(",:;")
        if target and (target in ("wendes", bot) or (bot and bot.startswith(target))):
            return Command("ask", rest.strip())
    if private:
        return Command("ask", text)
    return None


# ---- palavra de ativação por voz --------------------------------------------
_WORD = re.compile(r"[\wÀ-ÿ']+", re.UNICODE)


def words_of(text: str) -> list[str]:
    return _WORD.findall(_norm(text))


# Palavras comuns que o fuzzy confunde com a ativação ("vende" ≈ "vendes" = 91).
NOT_WAKE = {"vende", "vendem", "vendo", "venda", "vendas", "vender", "vendi", "lendo", "tendes"}
# Antes da ativação (2ª ou 3ª palavra), só interjeições: "Ô Wendes", "Olha, Wendes".
LEAD_INS = {
    "o", "oi", "ola", "olha", "ei", "e", "ai", "hey", "fala", "entao", "bom", "ta", "ok",
    "beleza", "so", "pois",
}


@dataclass
class VoiceRequest:
    text: str
    start: float
    speaker: str


class VoiceTrigger:
    """Enunciado cujas 1 a 3 primeiras palavras casam (fuzzy ≥ 85) com a palavra
    de ativação, e cujo restante tem pelo menos 3 palavras.

    Para cortar falsos positivos, só interjeições ("ô", "olha", "e aí"…) podem
    vir antes da ativação, e palavras comuns parecidas ("vende") não contam.

    Se o enunciado for só a palavra de ativação, a pergunta é o próximo
    enunciado do mesmo falante, ou o próximo em até 8 s se o falante for
    desconhecido. Entre ativações há um intervalo mínimo de 10 s.
    """

    def __init__(
        self,
        wake_words: list[str],
        *,
        threshold: int = 85,
        min_rest_words: int = 3,
        cooldown_s: float = 10.0,
        followup_s: float = 8.0,
        followup_known_s: float = 20.0,
    ) -> None:
        self.wake = [words_of(w) for w in wake_words if words_of(w)]
        self.threshold = threshold
        self.min_rest_words = min_rest_words
        self.cooldown_s = cooldown_s
        self.followup_s = followup_s
        self.followup_known_s = followup_known_s
        self._last_activation = float("-inf")
        self._armed: tuple[float, str] | None = None  # (fim do enunciado, falante)

    def _match(self, words: list[str]) -> int | None:
        """Índice da primeira palavra depois da ativação, ou None."""
        for i in range(min(3, len(words))):
            if i > 0 and words[i - 1] not in LEAD_INS:
                return None
            if words[i] in NOT_WAKE:
                continue
            for ww in self.wake:
                cand = " ".join(words[i : i + len(ww)])
                if len(words) - i >= len(ww) and fuzz.ratio(cand, " ".join(ww)) >= self.threshold:
                    return i + len(ww)
        return None

    def check(self, start: float, end: float, text: str, speaker: str = "") -> VoiceRequest | None:
        words = words_of(text)
        if self._armed is not None:
            armed_end, armed_speaker = self._armed
            limit = self.followup_known_s if armed_speaker else self.followup_s
            same = bool(armed_speaker) and speaker == armed_speaker
            if start - armed_end > limit:
                self._armed = None
            elif (same or not armed_speaker) and words:
                self._armed = None
                self._last_activation = start
                return VoiceRequest(text.strip(), start, speaker)
        if not words or start - self._last_activation < self.cooldown_s:
            return None
        idx = self._match(words)
        if idx is None:
            return None
        rest = words[idx:]
        if not rest:
            self._armed = (end, speaker)
            return None
        if len(rest) < self.min_rest_words:
            return None
        self._last_activation = start
        return VoiceRequest(_strip_wake(text, idx), start, speaker)


def _strip_wake(text: str, n_words: int) -> str:
    """Remove as `n_words` primeiras palavras do texto original (com acentos)."""
    matches = list(_WORD.finditer(text))
    if len(matches) <= n_words:
        return ""
    return text[matches[n_words].start():].strip()
