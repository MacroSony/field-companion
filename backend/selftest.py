#!/usr/bin/env python3
"""Fake phone: real-time cardinal + Chinese TTS, then exercise text and UI edits.
No audio is played, to avoid echo. No writes outside backend/ (even on import).
"""
import argparse
import ast
import asyncio
from collections import Counter
import json
from pathlib import Path
import time
import wave

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


def mixed_audio():
    with wave.open("/data/build/birding-demo/birds_cardinal.wav", "rb") as wav:
        assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (48000, 1, 2)
        bird = np.frombuffer(wav.readframes(20 * 48000), dtype="<i2").astype(np.float32) / 32768
    audio = np.zeros(20 * 48000, dtype=np.float32)
    audio[:len(bird)] = bird * 0.25
    text = "两只北美红雀在灌木丛里"
    speech = resample_poly(np.frombuffer(probe_tts(text), dtype="<i2").astype(np.float32) / 32768, 2, 1)
    start = 8 * 48000
    speech = speech[:len(audio) - start]
    audio[start:start + len(speech)] += speech * 0.85
    print(json.dumps({"event": "mixed_audio_ready", "seconds": 20, "tts": text,
                      "tts_start_s": 8, "tts_seconds": round(len(speech) / 48000, 3)}, ensure_ascii=False), flush=True)
    return (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()


async def run(url, extra_checks=True):
    pcm = await asyncio.to_thread(mixed_audio)
    counts = Counter()
    audio_bytes = 0
    assistant_finals = 0
    latest_items = []
    saw_logged = asyncio.Event()
    connected = asyncio.Event()
    async with websockets.connect(url, max_size=None) as ws:
        await ws.send(json.dumps({"type": "hello", "lat": 43.66, "lng": -79.39, "lang": "zh"}))

        async def receive():
            nonlocal audio_bytes, latest_items, assistant_finals
            async for raw in ws:
                if isinstance(raw, bytes):
                    audio_bytes += len(raw)
                    continue
                event = json.loads(raw)
                counts[event.get("type")] += 1
                print(json.dumps(event, ensure_ascii=False), flush=True)
                if event.get("type") == "state" and event.get("connected"):
                    connected.set()
                if event.get("type") == "transcript" and event.get("role") == "assistant" and event.get("final"):
                    assistant_finals += 1
                if event.get("type") == "checklist":
                    latest_items = event["items"]
                if event.get("type") == "earcon" and event.get("kind") == "logged":
                    saw_logged.set()

        receiver = asyncio.create_task(receive())
        try:
            try:
                await asyncio.wait_for(connected.wait(), 30)
            except asyncio.TimeoutError:
                print('{"event":"realtime_not_ready_streaming_anyway"}', flush=True)
            start = time.monotonic()
            frame_bytes = 1920 * 2
            for offset in range(0, len(pcm), frame_bytes):
                await asyncio.sleep(max(0, start + offset / 96000 - time.monotonic()))
                await ws.send(pcm[offset:offset + frame_bytes])
            # Trailing silence guarantees VAD can finalize the last utterance.
            for _ in range(50):
                await ws.send(bytes(frame_bytes))
                await asyncio.sleep(0.04)
            await asyncio.sleep(8)
            mixed_items = [dict(i) for i in latest_items]
            print(json.dumps({"event": "mixed_stream_result", "items": mixed_items,
                              "audio_bytes": audio_bytes, "messages": dict(counts)}, ensure_ascii=False), flush=True)
            if extra_checks:
                if latest_items:
                    item_id = latest_items[0]["id"]
                    await ws.send(json.dumps({"type": "edit", "id": item_id, "count": 3}))
                    await asyncio.sleep(0.5)
                    print(json.dumps({"event": "manual_edit_result", "items": latest_items}, ensure_ascii=False), flush=True)
                    await ws.send(json.dumps({"type": "delete", "id": item_id}))
                    await asyncio.sleep(0.5)
                    print(json.dumps({"event": "manual_delete_result", "items": latest_items}, ensure_ascii=False), flush=True)
                before = assistant_finals
                await ws.send(json.dumps({"type": "text", "text": "刚才那是什么鸟在叫？请查最近30秒的声音。"}, ensure_ascii=False))
                await asyncio.sleep(10)
                if assistant_finals == before:
                    print('{"event":"retry_text_after_no_answer"}', flush=True)
                    await ws.send(json.dumps({"type": "text", "text": "请用recent_sounds查最近60秒，告诉我听到了什么鸟。"}, ensure_ascii=False))
                    await asyncio.sleep(10)
            print(json.dumps({"event": "selftest_summary", "input_audio_bytes": len(pcm) + 50 * frame_bytes,
                              "audio_bytes": audio_bytes, "messages": dict(counts),
                              "mixed_stream_logged_two_cardinals": any(i["sci"] == "Cardinalis cardinalis" and i["count"] == 2 for i in mixed_items),
                              "final_items": latest_items}, ensure_ascii=False), flush=True)
        finally:
            receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="ws://127.0.0.1:8765/ws")
    parser.add_argument("--audio-only", action="store_true", help="Skip the extra UI/text checks")
    args = parser.parse_args()
    try:
        asyncio.run(run(args.url, not args.audio_only))
    except Exception as exc:
        # Don't print requests exceptions (could contain headers); keys never enter evidence.
        print(json.dumps({"event": "selftest_failed", "error_type": type(exc).__name__}), flush=True)
        raise SystemExit(1)
