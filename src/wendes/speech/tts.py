"""TTS com Piper (pt-BR). Sintetiza para WAV e cacheia frases fixas."""
from __future__ import annotations

import hashlib
import logging
import wave
from pathlib import Path

from piper import PiperVoice

from wendes.config import Config

log = logging.getLogger(__name__)


class Tts:
    def __init__(self, cfg: Config) -> None:
        self.voice = PiperVoice.load(str(cfg.resolve(cfg.tts.voice_path)))
        self.cache_dir = cfg.resolve(cfg.data_dir) / "tts_cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def synthesize(self, text: str) -> Path:
        """Sintetiza (com cache) e retorna o caminho do WAV."""
        key = hashlib.sha1(text.encode()).hexdigest()[:16]
        path = self.cache_dir / f"{key}.wav"
        if not path.exists():
            with wave.open(str(path), "wb") as w:
                self.voice.synthesize_wav(text, w)
            log.debug("TTS sintetizado: %r", text[:60])
        return path
