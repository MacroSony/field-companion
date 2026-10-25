"""Blocking local speaker HTTP client; call only in a background thread."""
import base64
import math

import numpy as np
import requests
from scipy.signal import resample_poly


def pcm16_at_16k(samples):
    """48 kHz mono float ring -> 16 kHz PCM16; ceil(N/3) samples."""
    audio = resample_poly(samples, 1, 3) if len(samples) else np.empty(0)
    return np.clip(np.rint(audio * 32768), -32768, 32767).astype("<i2").tobytes()


def speaker_request(url, action, samples, profile=None):
    payload = {"pcm16": base64.b64encode(pcm16_at_16k(samples)).decode()}
    if profile is not None:
        payload["profile"] = profile
    response = requests.post(url.rstrip("/") + "/" + action, json=payload, timeout=1.5)
    response.raise_for_status()
    result = response.json()
    if not isinstance(result, dict):
        raise ValueError("invalid speaker response")
    if result.get("ok") is False:
        raise ValueError(str(result.get("error", "speaker_failed")))
    if action == "score":
        scores = result["scores"]
        if not isinstance(scores, dict) or any(not isinstance(k, str) or not math.isfinite(v)
                or not -1.00001 <= v <= 1.00001 for k, v in scores.items()):
            raise ValueError("invalid speaker scores")
        if not math.isfinite(result["ms"]) or result["ms"] < 0:
            raise ValueError("invalid speaker timing")
    return result
