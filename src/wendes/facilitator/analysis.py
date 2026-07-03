"""Entendimento das respostas: rule-based primeiro, LLM (gemma) como refinamento."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from wendes.llm.client import Llm

log = logging.getLogger(__name__)

NO_BLOCKER_MARKERS = (
    "sem impedimento", "nenhum impedimento", "não tenho impedimento",
    "sem bloqueio", "nenhum bloqueio", "tudo certo", "tudo tranquilo",
    "não, ", "nada ",
)


@dataclass
class BlockerAnalysis:
    has_blocker: bool
    followup: str | None  # pergunta de follow-up, se fizer sentido


def analyze_blocker_answer(llm: Llm | None, name: str, answer: str) -> BlockerAnalysis:
    low = answer.lower().strip()
    if any(m in low for m in NO_BLOCKER_MARKERS) or low in ("não", "nao", "não."):
        return BlockerAnalysis(False, None)

    if llm is not None:
        resp = llm.ask_or_none(
            "Na daily, perguntei a {name} se há impedimentos de TRABALHO e a resposta foi: "
            '"{answer}". Responda em UMA única linha, exatamente neste formato:\n'
            "IMPEDIMENTO: sim | FOLLOWUP: nenhum\n"
            "Troque 'sim' por 'não' se não houver impedimento de trabalho, e troque "
            "'nenhum' por uma pergunta curta de esclarecimento apenas se ela ajudar a "
            "destravar o impedimento.".format(name=name, answer=answer[:500]),
            max_tokens=60,
        )
        if resp:
            low_resp = resp.lower()
            has = "impedimento: sim" in low_resp
            followup = None
            if has and "followup:" in low_resp:
                tail = resp[low_resp.index("followup:") + len("followup:"):].strip(" `*|")
                low_tail = tail.lower()
                valid = (
                    tail
                    and len(tail) < 200
                    and "nenhum" not in low_tail
                    and not tail.startswith("<")
                    and "pergunta curta" not in low_tail
                )
                if valid:
                    followup = tail
            return BlockerAnalysis(has, followup)

    # fallback conservador: considera que há impedimento, sem follow-up
    return BlockerAnalysis(True, None)
