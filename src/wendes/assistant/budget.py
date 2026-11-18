"""Orçamento de tokens para o LLM local (contexto pequeno, 8192 por padrão).

Estimativa grosseira e conservadora para pt-BR: tokens ≈ caracteres / 3,5.
`fit` monta as partes de um prompt respeitando o teto de cada uma e o total,
cortando primeiro a transcrição mais antiga.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

CHARS_PER_TOKEN = 3.5


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def lines_tokens(lines: list[str]) -> int:
    return sum(estimate_tokens(ln) + 1 for ln in lines)


@dataclass
class Part:
    """Uma parte do prompt. `keep="newest"` corta do começo (transcrição);
    `keep="first"` corta do fim (notas, trechos ordenados por relevância).
    `cut_order` menor = cortada antes quando o total estoura."""

    name: str
    lines: list[str] = field(default_factory=list)
    max_tokens: int = 0
    keep: str = "first"
    cut_order: int = 9


def _trim(lines: list[str], budget: int, keep: str) -> list[str]:
    if budget <= 0:
        return []
    out: list[str] = []
    used = 0
    seq = reversed(lines) if keep == "newest" else iter(lines)
    for ln in seq:
        cost = estimate_tokens(ln) + 1
        if used + cost > budget:
            break
        out.append(ln)
        used += cost
    return list(reversed(out)) if keep == "newest" else out


def fit(parts: list[Part], total: int) -> dict[str, list[str]]:
    """Corta cada parte no seu teto e, se o total estourar, corta pela `cut_order`."""
    trimmed = {p.name: _trim(p.lines, p.max_tokens or total, p.keep) for p in parts}
    excess = sum(lines_tokens(v) for v in trimmed.values()) - total
    for p in sorted(parts, key=lambda p: p.cut_order):
        if excess <= 0:
            break
        current = trimmed[p.name]
        have = lines_tokens(current)
        new = _trim(current, max(0, have - excess), p.keep)
        excess -= have - lines_tokens(new)
        trimmed[p.name] = new
    return trimmed
