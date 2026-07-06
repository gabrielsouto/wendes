"""Cliente do vLLM (OpenAI-compatible). O gemma vaza raciocínio em inglês se
não houver system prompt — sempre enviar a diretiva de resposta direta."""
from __future__ import annotations

import logging

from openai import OpenAI

from wendes.config import Config

log = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "Você é o assistente da daily da equipe{team}. Responda SEMPRE em "
    "português do Brasil, de forma direta e objetiva, sem mostrar raciocínio "
    "nem comentários em inglês."
)


class Llm:
    def __init__(self, cfg: Config) -> None:
        self.model = cfg.llm.model
        team = f" {cfg.team.name}" if cfg.team.name else ""
        self.system_prompt = SYSTEM_PROMPT.format(team=team)
        self.client = OpenAI(
            base_url=cfg.llm.base_url, api_key="none", timeout=cfg.llm.timeout_seconds
        )

    def ask(
        self,
        prompt: str,
        system: str | None = None,
        max_tokens: int = 1024,
        timeout: float | None = None,
        continue_on_length: bool = False,
    ) -> str:
        """Pergunta ao LLM; `timeout` sobrepõe o do config para chamadas longas (ata).

        Com `continue_on_length`, se a resposta estourar `max_tokens` faz uma
        rodada de continuação e concatena — a ata de reuniões longas não cabe
        numa resposta só. As chamadas ao vivo (orçamento curto de propósito)
        não usam isso.
        """
        client = self.client if timeout is None else self.client.with_options(timeout=timeout)
        messages = [
            {"role": "system", "content": system or self.system_prompt},
            {"role": "user", "content": prompt},
        ]
        resp = client.chat.completions.create(
            model=self.model, messages=messages, max_tokens=max_tokens
        )
        text = resp.choices[0].message.content or ""
        if continue_on_length and resp.choices[0].finish_reason == "length":
            log.info("Resposta atingiu max_tokens=%d — pedindo continuação", max_tokens)
            messages += [
                {"role": "assistant", "content": text},
                {
                    "role": "user",
                    "content": "Sua resposta foi cortada no limite de tamanho. "
                    "Continue EXATAMENTE de onde parou, sem repetir nada do que já escreveu.",
                },
            ]
            cont = client.chat.completions.create(
                model=self.model, messages=messages, max_tokens=max_tokens
            )
            text += cont.choices[0].message.content or ""
            if cont.choices[0].finish_reason == "length":
                log.warning("Resposta seguiu cortada mesmo após continuação")
        return text.strip()

    def ask_or_none(self, prompt: str, **kwargs) -> str | None:
        """Versão tolerante a falha — a facilitação ao vivo usa fallback rule-based."""
        try:
            return self.ask(prompt, **kwargs)
        except Exception as exc:  # noqa: BLE001
            log.warning("LLM indisponível (%s) — usando fallback", exc)
            return None
