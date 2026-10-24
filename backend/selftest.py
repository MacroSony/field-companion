#!/usr/bin/env python3
"""Live route-first mic/display protocol check using real-time TTS PCM.
No audio is played or saved; probe AST loading has no import side effects.
"""
import argparse
import ast
import asyncio
import json
from pathlib import Path
import time

import numpy as np
import requests
from scipy.signal import resample_poly
import websockets

from server import api_key

ROOT = Path(__file__).resolve().parents[1]


def probe_tts(text):
    # realtime_probe imports create probes/out. Execute its exact tts() definition
    # without executing module-level code, preserving the backend-only write boundary.
    tree = ast.parse((ROOT / "probes/realtime_probe.py").read_text())
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "tts")
    namespace = {"requests": requests, "API": "https://api.boson.ai/v1",
                 "H": {"Authorization": "Bearer " + api_key()}}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "probe_tts", "exec"), namespace)
    return namespace["tts"](text)


async def run(url):
    from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
    import uuid
    sid = "selftest-" + uuid.uuid4().hex[:8]
    parts = urlsplit(url)
    def endpoint(role, audio=False):
        query = dict(parse_qsl(parts.query))
        query.update(role=role, session=sid, audio="1" if audio else "0")
        return urlunsplit(parts._replace(query=urlencode(query)))

    cases = [("chatter", "Did you bring the sunscreen?"),
             ("sighting", "Two northern cardinals in the bushes"),
             ("question", "How many species are on our checklist?")]
    pcms = []
    for _, text in cases:
        raw = await asyncio.to_thread(probe_tts, text)
        pcm = resample_poly(np.frombuffer(raw, dtype="<i2").astype(np.float32), 2, 1)
        pcms.append(np.clip(pcm, -32768, 32767).astype("<i2").tobytes() + bytes(144000))
    events = {"mic": [], "display": []}
    audio = {"mic": 0, "display": 0}
    items = {"mic": [], "display": []}
    async with websockets.connect(endpoint("mic"), max_size=None) as mic, \
               websockets.connect(endpoint("display", True), max_size=None) as display:
        async def receive(ws, role):
            async for raw in ws:
                if isinstance(raw, bytes):
                    audio[role] += len(raw)
                    continue
                event = json.loads(raw)
                events[role].append(event)
                if event.get("type") == "checklist":
                    items[role] = event["items"]
                if event.get("type") != "transcript" or event.get("final"):
                    print(json.dumps({"client": role, **event}, ensure_ascii=False), flush=True)
        receivers = [asyncio.create_task(receive(mic, "mic")), asyncio.create_task(receive(display, "display"))]
        async def wait_for(predicate, timeout=25):
            end = time.monotonic() + timeout
            while not predicate():
                if time.monotonic() > end:
                    raise AssertionError("timed out waiting for protocol result")
                await asyncio.sleep(.05)
        try:
            await mic.send(json.dumps({"type": "hello", "lat": 43.66, "lng": -79.39, "lang": "en"}))
            await wait_for(lambda: any(e.get("connected") and e.get("echo_guard") for e in events["mic"]))
            for (label, text), pcm in zip(cases, pcms):
                before_audio = dict(audio)
                before_events = len(events["mic"])
                start = time.monotonic()
                for offset in range(0, len(pcm), 3840):
                    await asyncio.sleep(max(0, start + offset / 96000 - time.monotonic()))
                    await mic.send(pcm[offset:offset + 3840])
                await wait_for(lambda: any(e.get("type") == "transcript" and e.get("role") == "user"
                                          for e in events["mic"][before_events:]))
                if label == "sighting":
                    await wait_for(lambda: any(e.get("type") == "earcon" and e.get("kind") == "logged"
                                              for e in events["mic"][before_events:]))
                    await wait_for(lambda: bool(items["display"]) and items["display"] == items["mic"])
                    assert items["mic"][0]["count"] == 2
                    assert all(k in items["mic"][0] for k in ("en", "sci", "photo", "lat", "lng"))
                elif label == "question":
                    await wait_for(lambda: audio["mic"] > before_audio["mic"] and audio["display"] > before_audio["display"])
                await asyncio.sleep(3)
                if label != "question":
                    assert audio == before_audio, label + " unexpectedly spoke"
                print(json.dumps({"event": "router_case_pass", "case": label,
                                  "audio_bytes": {k: audio[k] - before_audio[k] for k in audio}}), flush=True)
            assert not any(e.get("type") == "error" for e in events["mic"])
            await mic.send(json.dumps({"type": "tap"}))
            await wait_for(lambda: any(e.get("type") == "flush" for e in events["mic"]))
            print(json.dumps({"event": "selftest_pass", "session": sid, "audio_bytes": audio,
                              "display_same_checklist": items["mic"] == items["display"],
                              "echo_guard": True}), flush=True)
        finally:
            for receiver in receivers:
                receiver.cancel()
            await asyncio.gather(*receivers, return_exceptions=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="ws://127.0.0.1:8791/ws")
    args = parser.parse_args()
    try:
        asyncio.run(run(args.url))
    except Exception as exc:
        print(json.dumps({"event": "selftest_failed", "error_type": type(exc).__name__}), flush=True)
        raise SystemExit(1)
