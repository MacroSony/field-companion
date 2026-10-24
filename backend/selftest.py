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
import socket
import threading
from contextlib import contextmanager

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


@contextmanager
def local_server():
    """Own only port 8792; use real Higgs and mock vision only if /classify is down."""
    import uvicorn
    import server as backend
    from unittest.mock import patch
    jpeg = Path("/data/build/birding-demo/img/cardinal.jpg").read_bytes()
    try:
        response = requests.post(__import__("os").environ.get("VISION_URL", "http://127.0.0.1:8795") + "/classify", json={
            "jpeg_b64": __import__("base64").b64encode(jpeg).decode(), "crop": None,
            "candidates": ["Cardinalis cardinalis", "Haemorhous mexicanus"], "k": 5}, timeout=30)
        response.raise_for_status()
        assert response.json().get("candidates")
        mocked = False
    except (requests.RequestException, ValueError, AssertionError):
        mocked = True
    print(json.dumps({"event": "vision_provider", "mocked": mocked}), flush=True)
    def mock_classify(jpeg, crop, candidates):
        return {"candidates": [{"sci": "Cardinalis cardinalis", "p": .95},
                               {"sci": "Haemorhous mexicanus", "p": .05}], "sharpness": 100, "ms": 1}
    provider = mock_classify if mocked else backend.classify_frame
    with patch.object(backend, "classify_frame", provider):
        # Bind first so an occupied test port fails without touching that process.
        sock = socket.socket()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 8792))
        server = uvicorn.Server(uvicorn.Config(backend.app, host="127.0.0.1", port=8792, log_level="warning"))
        thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started:
                if time.monotonic() > deadline:
                    raise RuntimeError("test server did not start")
                time.sleep(.05)
            yield
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            sock.close()


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
             ("look", "What is that?"),
             ("confirm", "Add it, two of them")]
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
                if label == "look":
                    await wait_for(lambda: any(e.get("type") == "capture" for e in events["mic"][before_events:]), timeout=60)
                    assert audio == before_audio, "look spoke before receiving frames"
                    capture = next(e for e in events["mic"][before_events:] if e.get("type") == "capture")
                    assert capture["n"] == 3
                    http_url = urlunsplit(parts._replace(scheme="http" if parts.scheme == "ws" else "https", path="/api/frame", query=""))
                    jpeg = Path("/data/build/birding-demo/img/cardinal.jpg").read_bytes()
                    response = await asyncio.to_thread(requests.post, http_url,
                        params={"session": sid, "obs": capture["obs"]}, data=jpeg,
                        headers={"Content-Type": "image/jpeg"}, timeout=90)
                    assert response.status_code == 200, "frame endpoint status " + str(response.status_code)
                    assert response.json()["detection"]["kind"] == "image"
                    await wait_for(lambda: any(e.get("type") == "observation" and any(d["kind"] == "image" for d in e["detections"])
                                              for e in events["mic"][before_events:]))
                    await wait_for(lambda: audio["mic"] > before_audio["mic"] and audio["display"] > before_audio["display"], timeout=45)
                    await wait_for(lambda: any(e.get("type") == "transcript" and e.get("role") == "assistant" and e.get("final")
                                              for e in events["mic"][before_events:]))
                    # Wait for estimated playback to end before sending next mic turn.
                    await wait_for(lambda: not next((e for e in reversed(events["mic"]) if e.get("type") == "state"), {}).get("speaking", True), timeout=30)
                    await asyncio.sleep(.5)
                elif label == "confirm":
                    await wait_for(lambda: any(e.get("type") == "earcon" and e.get("kind") == "logged"
                                              for e in events["mic"][before_events:]))
                    await wait_for(lambda: bool(items["display"]) and items["display"] == items["mic"])
                    assert items["mic"][0]["count"] == 2
                    assert all(k in items["mic"][0] for k in ("en", "sci", "photo", "lat", "lng"))
                    assert any(e.get("type") == "observation" and e.get("status") == "confirmed" and e.get("count") == 2
                               for e in events["mic"][before_events:])
                await asyncio.sleep(3)
                if label != "look":
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
    parser.add_argument("--url", default="ws://127.0.0.1:8792/ws")
    parser.add_argument("--serve", action="store_true", help="Own test server on 8792, mock vision only if /classify unavailable")
    args = parser.parse_args()
    try:
        if args.serve:
            if args.url != "ws://127.0.0.1:8792/ws":
                raise ValueError("--serve requires the default port-8792 URL")
            with local_server():
                asyncio.run(run(args.url))
        else:
            asyncio.run(run(args.url))
    except Exception as exc:
        print(json.dumps({"event": "selftest_failed", "error_type": type(exc).__name__}), flush=True)
        raise SystemExit(1)
