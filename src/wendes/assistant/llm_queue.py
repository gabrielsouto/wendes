"""Fila do LLM local (`LlmScheduler`) e saída estruturada (`ask_json`).

O LLM local atende uma requisição por vez (`llm.max_concurrency`, default 1).
Por isso, tudo passa por uma fila com prioridade:
  0 — pedido humano (pergunta, artefato, resumo);
  1 — notas incrementais;
  2 — reparo de Mermaid, compactação, classificação de pendências.
Jobs com a mesma `key` ainda na fila se fundem (o novo pedido devolve o futuro
do pendente). Cada chamada vai para `llm_calls.jsonl` (tipo, tokens estimados,
latência e ok/falha), sem o conteúdo.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from wendes.assistant import prompts_ptbr as P
from wendes.assistant.budget import estimate_tokens
from wendes.assistant.state import append_jsonl

log = logging.getLogger(__name__)

HUMAN = 0
NOTES = 1
BACKGROUND = 2

T = TypeVar("T", bound=BaseModel)


@dataclass(order=True)
class _Job:
    priority: int
    seq: int
    kind: str = field(compare=False)
    fn: Callable[[], Any] = field(compare=False)
    future: asyncio.Future = field(compare=False)
    key: str | None = field(compare=False, default=None)
    timeout: float = field(compare=False, default=120.0)
    est_tokens: int = field(compare=False, default=0)


class LlmScheduler:
    def __init__(
        self,
        max_concurrency: int = 1,
        timeout_s: float = 120.0,
        telemetry_path: Path | None = None,
        on_change: Callable[[int], None] | None = None,
    ) -> None:
        self.max_concurrency = max(1, max_concurrency)
        self.timeout_s = timeout_s
        self.telemetry_path = telemetry_path
        self.on_change = on_change
        self._queue: asyncio.PriorityQueue[_Job] = asyncio.PriorityQueue()
        self._seq = itertools.count()
        self._pending_keys: dict[str, _Job] = {}
        self._running = 0
        self._workers: list[asyncio.Task] = []
        self._idle = asyncio.Event()
        self._idle.set()
        self.order: list[str] = []  # tipos na ordem em que rodaram (testes/diagnóstico)

    @property
    def size(self) -> int:
        return self._queue.qsize() + self._running

    def _changed(self) -> None:
        if self.size:
            self._idle.clear()
        else:
            self._idle.set()
        if self.on_change is not None:
            self.on_change(self.size)

    def submit(
        self,
        priority: int,
        kind: str,
        fn: Callable[[], Any],
        *,
        key: str | None = None,
        timeout: float | None = None,
        est_tokens: int = 0,
    ) -> asyncio.Future:
        if key is not None and key in self._pending_keys:
            return self._pending_keys[key].future
        fut = asyncio.get_running_loop().create_future()
        job = _Job(priority, next(self._seq), kind, fn, fut, key,
                   timeout or self.timeout_s, est_tokens)
        if key is not None:
            self._pending_keys[key] = job
        self._queue.put_nowait(job)
        self._changed()
        return fut

    def start(self) -> None:
        if not self._workers:
            self._workers = [
                asyncio.create_task(self._worker(), name=f"llm-{i}")
                for i in range(self.max_concurrency)
            ]

    async def _worker(self) -> None:
        while True:
            job = await self._queue.get()
            if job.key is not None and self._pending_keys.get(job.key) is job:
                del self._pending_keys[job.key]
            if job.future.cancelled():
                self._changed()
                continue
            self._running += 1
            self._changed()
            self.order.append(job.kind)
            t0 = time.monotonic()
            ok = False
            try:
                result = await asyncio.wait_for(asyncio.to_thread(job.fn), job.timeout)
                ok = True
                if not job.future.done():
                    job.future.set_result(result)
            except asyncio.CancelledError:
                if not job.future.done():
                    job.future.cancel()
                raise
            except Exception as exc:  # noqa: BLE001 — a falha vai para quem pediu
                if not job.future.done():
                    job.future.set_exception(exc)
                log.warning("Chamada ao LLM (%s) falhou: %s", job.kind, type(exc).__name__)
            finally:
                self._running -= 1
                self._telemetry(job, time.monotonic() - t0, ok)
                self._changed()

    def _telemetry(self, job: _Job, latency: float, ok: bool) -> None:
        if self.telemetry_path is None:
            return
        try:
            append_jsonl(self.telemetry_path, {
                "t": round(time.time(), 1),
                "tipo": job.kind,
                "prioridade": job.priority,
                "tokens_est": job.est_tokens,
                "latencia_s": round(latency, 2),
                "ok": ok,
            })
        except OSError:
            log.debug("Falha ao gravar a telemetria do LLM", exc_info=True)

    async def join(self, timeout: float | None = None) -> None:
        """Espera a fila esvaziar e os jobs em curso terminarem."""
        await asyncio.wait_for(self._idle.wait(), timeout)

    async def close(self) -> None:
        while not self._queue.empty():
            job = self._queue.get_nowait()
            job.future.cancel()
        self._pending_keys.clear()
        for w in self._workers:
            w.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers = []
        self._running = 0
        self._changed()


# ---- saída estruturada ------------------------------------------------------
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> Any:
    """Primeiro objeto JSON da resposta, tolerando cercas ```json e texto em volta."""
    if not text:
        raise ValueError("resposta vazia")
    candidates = [m.group(1) for m in _FENCE.finditer(text)] + [text]
    decoder = json.JSONDecoder()
    for cand in candidates:
        start = cand.find("{")
        while start != -1:
            try:
                obj, _ = decoder.raw_decode(cand[start:])
                return obj
            except json.JSONDecodeError:
                start = cand.find("{", start + 1)
    raise ValueError("nenhum objeto JSON na resposta")


def structured_kwargs(mode: str, schema: type[BaseModel], vllm_key: str = "guided_json") -> dict:
    if mode == "json_object":
        return {"response_format": {"type": "json_object"}}
    if mode == "vllm_guided":
        js = schema.model_json_schema()
        if vllm_key == "structured_outputs":
            return {"extra_body": {"structured_outputs": {"json": js}}}
        return {"extra_body": {"guided_json": js}}
    if mode == "ollama_format":
        return {"extra_body": {"format": schema.model_json_schema()}}
    return {}


def _short_error(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        errs = exc.errors()[:3]
        return "; ".join(f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in errs)
    return str(exc)[:200]


def ask_json(
    llm,
    prompt: str,
    schema: type[T],
    *,
    system: str | None = None,
    mode: str = "json_object",
    vllm_key: str = "guided_json",
    max_tokens: int = 600,
) -> T | None:
    """Pergunta ao LLM e valida a resposta com pydantic.

    Uma nova tentativa com o erro de validação; se falhar de novo, None (cada
    funcionalidade decide o fallback). Erros de rede/servidor sobem.
    """
    kwargs = structured_kwargs(mode, schema, vllm_key)
    full = f"{prompt}\n\n{P.JSON_ONLY}"
    current = full
    for attempt in range(2):
        raw = llm.ask(current, system=system, max_tokens=max_tokens, **kwargs)
        try:
            return schema.model_validate(extract_json(raw))
        except (ValueError, ValidationError) as exc:
            err = _short_error(exc)
            log.info(
                "JSON inválido do LLM (%s, tentativa %d): %s", schema.__name__, attempt + 1, err
            )
            current = f"{full}\n\n{P.RETRY.format(erro=err)}"
    return None


def est(prompt: str, max_tokens: int = 0) -> int:
    return estimate_tokens(prompt) + max_tokens
