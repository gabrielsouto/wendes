"""Todas as frases faladas pelo bot (pt-BR), templatizadas.

PHONETIC corrige pronúncia do Piper para termos problemáticos — acrescente
os jargões da sua equipe em `tts.phonetic` no config.yaml conforme ouvir erros.
"""
from __future__ import annotations

PHONETIC = {
    "daily": "deili",
}


def spoken(text: str, extra: dict[str, str] | None = None) -> str:
    for term, sound in {**PHONETIC, **(extra or {})}.items():
        text = text.replace(term, sound)
    return text


OPENING = "Bom dia, pessoal! Aqui é o robô da daily. Vamos começar a nossa reunião."
ROLL_CALL = "Hoje temos na sala: {names}."
ABSENT = "Não localizei: {names}. Sigo com quem está presente."
ASK_PERSON = "{name}, sua vez. {question}"
NEXT_QUESTION = "{question}"
FOLLOWUP_BLOCKER = "Entendi, {name}. {followup}"
THANKS_NEXT = "Obrigado, {name}."
TIMEBOX_WARNING = (
    "{name}, só um instante — para mantermos o tempo, você pode concluir em uma frase?"
)
NO_ANSWER = "Acho que o {name} não está podendo falar agora. Sigo adiante."
CLOSING = "É isso, pessoal! Obrigado a todos. A ata será enviada em instantes. Boa sprint!"
WAITING = "Ainda estou aguardando mais participantes para começar."
WAIT_COMMAND = "Bom dia! Quando estiverem prontos, é só enviar o comando barra comece no chat."
SKIPPED = "Certo, seguindo em frente."
ACK = "Certo."  # feedback imediato ao fim da fala, enquanto transcreve
