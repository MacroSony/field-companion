"""Explicit, server-side Realtime selection; credentials never reach the phone."""
from dataclasses import dataclass
import os
from pathlib import Path
from urllib.parse import urlencode

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
HIGGS_MODEL = "higgs-realtime-1.5-preview"


def local_values():
    try:
        return dotenv_values(ROOT / ".env.local")
    except (OSError, UnicodeError):
        return {}


@dataclass(frozen=True)
class RealtimeConfig:
    provider: str = "higgs"
    model: str = HIGGS_MODEL
    transcription_model: str = "higgs-stt-3.1"
    voice: str = ""

    @classmethod
    def load(cls):
        local = local_values()

        def value(name, default):
            return (os.environ.get(name) or local.get(name) or default).strip()

        provider = value("REALTIME_PROVIDER", "higgs").lower()
        if provider == "higgs":
            return cls()
        if provider != "openai":
            raise ValueError("REALTIME_PROVIDER must be higgs or openai")
        return cls(provider="openai", model=value("OPENAI_REALTIME_MODEL", "gpt-realtime-2.1"),
                   transcription_model=value("OPENAI_REALTIME_TRANSCRIPTION_MODEL", "gpt-transcribe"),
                   voice=value("OPENAI_REALTIME_VOICE", "marin"))

    @property
    def url(self):
        host = "api.openai.com" if self.provider == "openai" else "api.boson.ai"
        return "wss://" + host + "/v1/realtime?" + urlencode({"model": self.model})

    def openai_key(self):
        key = (os.environ.get("OPENAI_API_KEY") or local_values().get("OPENAI_API_KEY") or "").strip()
        if not key:
            raise RuntimeError("OpenAI Realtime needs the project's OPENAI_API_KEY")
        return key

    def phase(self, common):
        if self.provider != "openai":
            return common
        # GA session updates require the type discriminator. The model is selected
        # in the connection URL and is not changed by later route/answer updates.
        return {"type": "realtime", **common, "parallel_tool_calls": False,
                "reasoning": {"effort": "low"}, "max_output_tokens": 1024}

    def session(self, common):
        config = {**common, "audio": {
            "input": {"format": {"type": "audio/pcm", "rate": 24000},
                      "turn_detection": {"type": "server_vad", "threshold": 0.5,
                                         "prefix_padding_ms": 300, "silence_duration_ms": 600,
                                         "create_response": False, "interrupt_response": False},
                      "transcription": {"model": self.transcription_model}},
            "output": {"format": {"type": "audio/pcm", "rate": 24000}}}}
        if self.provider == "openai":
            config["audio"]["input"]["noise_reduction"] = None
            config["audio"]["output"]["voice"] = self.voice
        else:
            config["model"] = self.model
        return config

    def accepts_initial(self, event, expected):
        if self.provider != "openai":
            return event.get("type") in ("session.created", "session.updated")
        if event.get("type") != "session.updated":
            return False
        actual = event.get("session", {})
        if any(actual.get(k) != expected[k] for k in ("instructions", "tool_choice", "output_modalities")):
            raise RuntimeError("OpenAI Realtime did not confirm the requested routing configuration")
        audio = actual.get("audio", {})
        for side in ("input", "output"):
            if audio.get(side, {}).get("format") != expected["audio"][side]["format"]:
                raise RuntimeError("OpenAI Realtime did not confirm 24 kHz PCM")
        incoming = audio.get("input", {})
        if incoming.get("transcription", {}).get("model") != self.transcription_model:
            raise RuntimeError("OpenAI Realtime did not confirm input transcription")
        detection = incoming.get("turn_detection", {})
        if any(detection.get(flag) is not False for flag in ("create_response", "interrupt_response")):
            raise RuntimeError("OpenAI Realtime must leave response and interruption control to the backend")
        if audio.get("output", {}).get("voice") != self.voice:
            raise RuntimeError("OpenAI Realtime did not confirm the requested voice")
        return True
