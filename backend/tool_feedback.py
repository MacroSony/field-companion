"""Short, local Higgs recordings for feedback before slow read-only tools.

Regenerate explicitly with: python -B backend/tool_feedback.py --generate
Runtime playback never makes a synthesis/model request.
"""
from functools import lru_cache
from pathlib import Path
import wave


CLIPS = {
    ("look", "en"): "Let me take a look.",
    ("look", "zh"): "好的，我看一下。",
    ("check", "en"): "Let me check.",
    ("check", "zh"): "好的，我查一下。",
}
TOOLS = {"look": "look", "new_observation": "look",
         "nearby_species": "check", "describe": "check"}
DIRECTORY = Path(__file__).with_name("audio")


@lru_cache(maxsize=4)
def recording(kind, language):
    text = CLIPS[kind, language]
    with wave.open(str(DIRECTORY / f"{kind}-{language}.wav"), "rb") as source:
        if (source.getnchannels(), source.getsampwidth(), source.getframerate()) != (1, 2, 24000):
            raise ValueError("Expected mono 24 kHz PCM16 feedback")
        if not 0 < source.getnframes() <= 4 * 24000:
            raise ValueError("Feedback must be at most four seconds")
        pcm = source.readframes(source.getnframes())
        if len(pcm) != 2 * source.getnframes():
            raise ValueError("Truncated feedback recording")
    return text, pcm


def generate():
    import requests
    from server import api_key
    key = api_key()
    DIRECTORY.mkdir(exist_ok=True)
    for (kind, language), text in CLIPS.items():
        with requests.post("https://api.boson.ai/v1/audio/speech",
                headers={"Authorization": "Bearer " + key},
                json={"model": "higgs-tts-3", "input": text, "voice": "default",
                      "stream": True, "response_format": "pcm"},
                stream=True, timeout=(5, 15)) as response:
            response.raise_for_status()
            data = bytearray()
            for chunk in response.iter_content(4096):
                data.extend(chunk)
                if len(data) > 4 * 48000:
                    raise ValueError("Synthesis exceeded feedback duration limit")
        if not data or len(data) % 2:
            raise ValueError("Invalid synthesis PCM")
        path = DIRECTORY / f"{kind}-{language}.wav"
        with wave.open(str(path), "wb") as target:
            target.setnchannels(1)
            target.setsampwidth(2)
            target.setframerate(24000)
            target.writeframes(data)
        print(f"{path.name}: {len(data) / 48000:.2f}s")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--generate", action="store_true")
    if parser.parse_args().generate:
        generate()
