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

    def ask(self, prompt: str, system: str | None = None, max_tokens: int = 1024) -> str:
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system or self.system_prompt},
                {"role": "user", "content": prompt},
            ],
            max_tokens=max_tokens,
        )
        return (resp.choices[0].message.content or "").strip()

    def ask_or_none(self, prompt: str, **kwargs) -> str | None:
        """Versão tolerante a falha — a facilitação ao vivo usa fallback rule-based."""
        try:
            return self.ask(prompt, **kwargs)
        except Exception as exc:  # noqa: BLE001
            log.warning("LLM indisponível (%s) — usando fallback", exc)
            return None
