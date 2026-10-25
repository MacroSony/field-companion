"""Opt-in real Realtime check with synthetic input and fixture species data.

Uses the project's configured key; never prints credentials or plays audio.
BirdNET and taxonomy lookups are fixtures. No photo classifier is called.
"""
import argparse
import asyncio
import json
from pathlib import Path
import time
import wave
from unittest.mock import AsyncMock, patch

import server as backend
from expert import Settings
from realtime_provider import RealtimeConfig


class Phone:
    def __init__(self):
        self.events = []
        self.audio_bytes = 0

    async def send_json(self, event):
        self.events.append(event)

    async def send_bytes(self, data):
        self.audio_bytes += len(data)


async def check(args, report):
    config = RealtimeConfig.load()
    report.update(provider=config.provider, model=config.model,
                  fixtures=["taxonomy", "BirdNET", "in-progress photo task"],
                  real_services=["Realtime WebSocket, routing, input transcription and speech output"],
                  physical_phone_test=False, checks=[])
    session = backend.Session("realtime-smoke", realtime=config)
    session.expert.settings = Settings(enabled=False)
    session.bird_window = AsyncMock()
    session.allowed_species = lambda: {"Anas platyrhynchos"}
    phone = Phone()
    client = backend.Client(phone, session)
    session.clients.add(client)
    session.mic = client
    logs = []

    def log(event, **fields):
        logs.append({"event": event, **fields})

    session.log = log

    async def wait_for(predicate, label, timeout=45):
        end = time.monotonic() + timeout
        while not predicate():
            failures = [e for e in phone.events if e.get("type") == "error"]
            if failures:
                raise AssertionError(label + ": backend reported " + failures[-1].get("message", "error"))
            if time.monotonic() >= end:
                raise AssertionError(label + ": timed out")
            await asyncio.sleep(.05)

    async def idle(label):
        await wait_for(lambda: not session.pending and not session.turn_in_progress
                       and not session.active and time.monotonic() >= session.play_until,
                       label)

    async def turn(label, text, expected):
        start = len(logs)
        before = phone.audio_bytes
        await session.handle_phone({"type": "text", "text": text}, client)
        await wait_for(lambda: any(e["event"] == "route_done" for e in logs[start:]), label)
        await idle(label)
        routes = [e["tools"] for e in logs[start:] if e["event"] == "route_done"]
        assert len(routes) == 1 and len(routes[0]) == 1 and routes[0][0] in expected, (label, routes)
        result = {"case": label, "tools": routes[0], "passed": True,
                  "audio_bytes": phone.audio_bytes - before}
        report["checks"].append(result)
        print(json.dumps(result), flush=True)

    mallard = {"sci": "Anas platyrhynchos", "en": "Mallard", "zh": "绿头鸭", "photo": "", "group": "Aves"}
    try:
        with patch.object(backend, "resolve", return_value=mallard):
            session.start()
            await wait_for(lambda: session.connected, "connect")
            if args.audio_input:
                with wave.open(str(args.audio_input), "rb") as source:
                    assert (source.getframerate(), source.getnchannels(), source.getsampwidth()) == (48000, 1, 2)
                    pcm = source.readframes(source.getnframes()) + bytes(96000)
                before = phone.audio_bytes
                begin = time.monotonic()
                for offset in range(0, len(pcm), 3840):
                    await asyncio.sleep(max(0, begin + offset / 96000 - time.monotonic()))
                    await session.ingest_audio(pcm[offset:offset + 3840])
                await wait_for(lambda: any(e.get("role") == "user" and e.get("final") for e in phone.events), "synthetic speech input")
                await wait_for(lambda: any(e["event"] == "route_done" for e in logs), "audio route")
                await idle("audio answer")
                assert phone.audio_bytes > before
                routes = [e["tools"] for e in logs if e["event"] == "route_done"]
                assert routes == [["speak"]], routes
                result = {"case": "synthetic microphone input and spoken answer", "passed": True,
                          "tools": routes[0], "audio_bytes": phone.audio_bytes - before}
                report["checks"].append(result)
                print(json.dumps(result), flush=True)
            else:
                await turn("spoken mic-check reply", "Can you hear me?", {"speak"})
                assert phone.audio_bytes > 0

            await turn("voice checklist logging", "Two mallards. Add them to my checklist.", {"add_observation"})
            assert len(session.items) == 1 and session.items[0]["count"] == 2
            await turn("new animal opens camera", "I see another animal. Could you help me identify it?", {"look", "new_observation"})
            request = session.camera_request
            assert request and request["phase"] == "opening"
            assert not any(e.get("type") == "capture" for e in phone.events)
            await session.camera_ready({"request_id": request["id"]})
            await idle("camera ready confirmation")
            assert request["phase"] == "ready"
            assert not any(e.get("type") == "capture" for e in phone.events)
            await session.handle_phone({"type": "text", "text": "start"}, client)
            await wait_for(lambda: any(e.get("type") == "capture" for e in phone.events), "start capture")
            report["checks"].append({"case": "camera waits for start before capture", "passed": True})
            await idle("capture acknowledgement")
            request["phase"] = "classifying"
            observation = session.observations[request["obs"]]
            observation.analysis_status = "analyzing"
            observation.analysis_stage = "recognizing"
            before = len(phone.events)
            await turn("identification progress", "Check the identification progress.", {"get_task_status", "get_observation"})
            assert session.camera_request is request and request["phase"] == "classifying"
            assert not any("camera ready" in e.get("text", "").lower() for e in phone.events[before:])
            report["passed"] = True
    finally:
        await session.close()
        report["backend_errors"] = [e.get("message", "") for e in phone.events if e.get("type") == "error"]
        report["input_transcripts"] = [e["text"] for e in phone.events if e.get("role") == "user" and e.get("final")]
        report["output_audio_bytes"] = phone.audio_bytes
        report["events"] = logs


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio-input", type=Path, help="Synthetic 48 kHz mono PCM16 WAV; not a physical phone test")
    parser.add_argument("--report", type=Path, default=Path("out/gpt-realtime/smoke.json"))
    args = parser.parse_args()
    result = {"passed": False}
    try:
        asyncio.run(check(args, result))
    except Exception as exc:
        result["error_type"] = type(exc).__name__
        # Our assertions contain only synthetic inputs and public protocol state.
        if isinstance(exc, AssertionError):
            result["failure"] = str(exc)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": result["passed"], "report": str(args.report),
                      "failure": result.get("failure"), "error_type": result.get("error_type")}), flush=True)
    raise SystemExit(0 if result["passed"] else 1)
