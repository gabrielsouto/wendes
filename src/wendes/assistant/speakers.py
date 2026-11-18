"""Atribuição de falas pelo nível de áudio de cada participante do Jitsi.

A linha do tempo usa o relógio da gravação (amostras gravadas / 16 kHz),
nunca o relógio de parede. Cada evento de nível é guardado como
`(t_audio, pid, level)`, com `t_audio = posição da captura - latency_offset_s`.

A atribuição de um segmento soma a energia de cada participante no intervalo
`[start, end]`: o vencedor precisa de pelo menos `min_share` da energia total e
de `min_samples` amostras com som; senão o falante fica "" (não identificado).
Nada no modo assist depende disto: com `assistant.speakers.enabled: false` todo
segmento sai sem falante. A Fase 0 (docs/SPIKE_FALANTES.md) mede a precisão.

Não usa a heurística antiga de "dominant speaker no ponto médio do segmento".
"""
from __future__ import annotations

import json
import logging
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# Nível abaixo disso é ruído/silêncio e não conta como amostra com som.
SOUND_LEVEL = 0.01
# Quanto da linha do tempo fica em memória (o arquivo guarda tudo).
KEEP_SECONDS = 900.0


@dataclass(frozen=True)
class LevelEvent:
    t: float
    pid: str
    level: float


@dataclass(frozen=True)
class Word:
    start: float
    end: float
    text: str


@dataclass
class SpeakerSpan:
    start: float
    end: float
    text: str
    pid: str
    conf: float


def attribute_levels(
    events: Iterable[LevelEvent],
    start: float,
    end: float,
    min_share: float = 0.6,
    min_samples: int = 3,
) -> tuple[str, float]:
    """(pid vencedor, fração da energia) no intervalo; pid "" se não confiável."""
    energy: dict[str, float] = {}
    samples: dict[str, int] = {}
    for ev in events:
        if ev.t < start or ev.t > end or ev.level <= 0:
            continue
        energy[ev.pid] = energy.get(ev.pid, 0.0) + ev.level
        if ev.level >= SOUND_LEVEL:
            samples[ev.pid] = samples.get(ev.pid, 0) + 1
    total = sum(energy.values())
    if total <= 0:
        return "", 0.0
    pid, best = max(energy.items(), key=lambda kv: kv[1])
    share = best / total
    if share < min_share or samples.get(pid, 0) < min_samples:
        return "", share
    return pid, share


def dominant_at(dominant: Sequence[tuple[float, str]], t: float) -> str:
    """Heurística antiga (só para comparação na Fase 0): dominant speaker em t."""
    current = ""
    for ts, pid in dominant:
        if ts > t:
            break
        current = pid
    return current


def split_by_speaker(
    words: Sequence[Word],
    events: Sequence[LevelEvent],
    min_switch_s: float = 1.0,
    min_share: float = 0.6,
    min_samples: int = 3,
) -> list[SpeakerSpan]:
    """Quebra uma sequência de palavras onde o dominante mudar por ≥ min_switch_s.

    Cada palavra recebe o participante de maior energia na sua janela; trechos
    de um mesmo participante mais curtos que `min_switch_s` são absorvidos pelo
    trecho anterior (ou pelo seguinte, no começo). Cada trecho final é atribuído
    de novo com `attribute_levels`, com as mesmas regras de confiança.
    """
    if not words:
        return []
    labels: list[str] = []
    for w in words:
        pid, _ = attribute_levels(events, w.start - 0.1, w.end + 0.1, 0.0, 1)
        labels.append(pid)
    runs: list[list] = []  # [pid, start, end, [words]]
    for w, pid in zip(words, labels):
        if runs and runs[-1][0] == pid:
            runs[-1][2] = w.end
            runs[-1][3].append(w)
        else:
            runs.append([pid, w.start, w.end, [w]])
    merged: list[list] = []
    for run in runs:
        short = (run[2] - run[1]) < min_switch_s
        if merged and (short or merged[-1][0] == run[0]):
            merged[-1][2] = run[2]
            merged[-1][3].extend(run[3])
        else:
            merged.append(run)
    # um trecho inicial curto vai para o seguinte
    if len(merged) > 1 and (merged[0][2] - merged[0][1]) < min_switch_s:
        first = merged.pop(0)
        merged[0][1] = first[1]
        merged[0][3] = first[3] + merged[0][3]
    out: list[SpeakerSpan] = []
    for _, start, end, ws in merged:
        pid, conf = attribute_levels(events, start, end, min_share, min_samples)
        text = " ".join(w.text.strip() for w in ws).strip()
        out.append(SpeakerSpan(start, end, text, pid, conf))
    return out


def load_levels(
    path: Path, offset: float = 0.0
) -> tuple[list[LevelEvent], list[tuple[float, str]], dict[str, str]]:
    """Lê audio_levels.jsonl → (níveis, dominant speakers, pid→nome).

    `offset` é subtraído dos tempos gravados (os arquivos do spike guardam o
    tempo bruto, sem offset; os da sessão já vêm com o offset aplicado).
    """
    levels: list[LevelEvent] = []
    dominant: list[tuple[float, str]] = []
    names: dict[str, str] = {}
    if not path.exists():
        return levels, dominant, names
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue  # última linha cortada se o processo morreu
        if "name" in row:
            names[str(row["pid"])] = str(row["name"])
        elif row.get("dominant"):
            dominant.append((float(row["t"]) - offset, str(row["pid"])))
        elif "level" in row:
            t = float(row["t"]) - offset
            levels.append(LevelEvent(t, str(row["pid"]), float(row["level"])))
    levels.sort(key=lambda e: e.t)
    dominant.sort()
    return levels, dominant, names


class SpeakerTracker:
    """Linha do tempo de nível de áudio por participante, em memória e em arquivo."""

    def __init__(
        self,
        position_fn: Callable[[], float],
        *,
        latency_offset_s: float = 0.0,
        min_share: float = 0.6,
        min_samples: int = 3,
        log_path: Path | None = None,
        name_fn: Callable[[str], str] | None = None,
        keep_s: float = KEEP_SECONDS,
    ) -> None:
        self.position_fn = position_fn
        self.offset = latency_offset_s
        self.min_share = min_share
        self.min_samples = min_samples
        self.name_fn = name_fn or (lambda pid: pid)
        self.keep_s = keep_s
        self.paused = False
        self.names: dict[str, str] = {}
        self._events: deque[LevelEvent] = deque()
        self._file = open(log_path, "a", encoding="utf-8") if log_path else None  # noqa: SIM115

    def now(self) -> float:
        return self.position_fn() - self.offset

    def _write(self, row: dict) -> None:
        if self._file is not None:
            self._file.write(json.dumps(row, ensure_ascii=False) + "\n")

    def set_name(self, pid: str, name: str) -> None:
        if name and self.names.get(pid) != name:
            self.names[pid] = name
            self._write({"pid": pid, "name": name})

    def on_levels(self, items: Iterable[tuple[str, float]]) -> None:
        if self.paused:
            return
        t = round(self.now(), 3)
        for pid, level in items:
            try:
                lv = float(level)
            except (TypeError, ValueError):
                continue
            self._events.append(LevelEvent(t, str(pid), lv))
            self._write({"t": t, "pid": str(pid), "level": round(lv, 4)})
        self._prune(t)
        if self._file is not None:
            self._file.flush()

    def on_dominant(self, pid: str, display_name: str = "") -> None:
        if display_name:
            self.set_name(pid, display_name)
        if not self.paused:
            self._write({"t": round(self.now(), 3), "pid": pid, "dominant": True})

    def _prune(self, t: float) -> None:
        limit = t - self.keep_s
        while self._events and self._events[0].t < limit:
            self._events.popleft()

    def attribute(self, start: float, end: float) -> tuple[str, float]:
        """(nome canônico, confiança) do segmento; ("", conf) se não identificado."""
        pid, conf = attribute_levels(self._events, start, end, self.min_share, self.min_samples)
        if not pid:
            return "", round(conf, 3)
        return self.name_fn(self.names.get(pid, pid)), round(conf, 3)

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None
