#!/usr/bin/env python3
"""Real-time synthetic-mic fan-out; writes evidence only to docs/fanout-test.md.
Run: FIELD_COMPANION_TEST_DATA=/path/to/fixtures python -B probes/fanout_probe.py
No audio playback: output audio is counted, not fed back into the microphone.
"""
import os
os.environ.setdefault("NUMBA_DISABLE_JIT", "1")
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
from pathlib import Path
import time
import wave

import numpy as np
from scipy.signal import resample_poly, firwin, lfilter
from birdnetlib import RecordingBuffer
from birdnetlib.analyzer import Analyzer
from realtime_probe import connect, tts

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "docs/fanout-test.md"
DATA = Path(os.environ.get("FIELD_COMPANION_TEST_DATA", str(ROOT / "testdata"))).expanduser()
SR = 48000
FRAME = 1920
DURATION = 66
UTTERANCES = [(8, "两只北美红雀在灌木丛里"), (30, "刚才那是什么在叫？"),
              (50, "how many species do we have?")]
TOOLS = [
    {"type": "function", "name": "add_observation", "description": "Add a user-confirmed bird observation to the fake checklist; not for automatic BirdNET notifications.",
     "parameters": {"type": "object", "properties": {"species": {"type": "string"}, "count": {"type": "integer"}}, "required": ["species", "count"]}},
    {"type": "function", "name": "get_checklist", "description": "Return the user-confirmed fake checklist.",
     "parameters": {"type": "object", "properties": {}}},
    {"type": "function", "name": "recent_sounds", "description": "Return timestamped BirdNET detections from the last N seconds of the microphone stream; not confirmed sightings.",
     "parameters": {"type": "object", "properties": {"seconds": {"type": "number"}}, "required": ["seconds"]}},
]


def evidence(event_name, **fields):
    line = json.dumps({"event": event_name, **fields}, ensure_ascii=False, allow_nan=False)
    print(line, flush=True)
    with REPORT.open("a") as f:
        f.write("- `" + line.replace("`", "'") + "`\n")


def make_stream():
    missing = [str(DATA / f"birds_{name}.wav") for name in ("cardinal", "robin")
               if not (DATA / f"birds_{name}.wav").is_file()]
    if missing:
        print("SKIP fanout probe: missing external fixtures: " + ", ".join(missing)
              + "; set FIELD_COMPANION_TEST_DATA.", flush=True)
        raise SystemExit(0)
    audio = np.zeros(SR * DURATION, dtype=np.float32)
    for name, start, seconds in [("cardinal", 0, 40), ("robin", 40, 20)]:
        with wave.open(str(DATA / f"birds_{name}.wav"), "rb") as w:
            assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (SR, 1, 2)
            x = np.frombuffer(w.readframes(SR * seconds), dtype="<i2").astype(np.float32) / 32768
        audio[start * SR:start * SR + len(x)] = x * 0.25
    overlays = []
    for start, text in UTTERANCES:
        pcm = tts(text)
        x = resample_poly(np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768, 2, 1)
        end = start + len(x) / SR
        if end > DURATION:
            raise ValueError("TTS exceeds stream duration")
        audio[start * SR:start * SR + len(x)] += x * 0.85
        active = np.flatnonzero(np.abs(x) > 500 / 32768)
        overlays.append({"start": start, "end": round(end, 3), "text": text,
                         "active_end": round(start + active[-1] / SR, 3) if len(active) else end})
        evidence("tts_ready", **overlays[-1])
    evidence("mixed_stream", duration_s=DURATION, background_gain=0.25, tts_gain=0.85,
             clipped_samples=int(np.count_nonzero(np.abs(audio) > 1)), background_segments=[
                 {"species": "Northern Cardinal", "start": 0, "end": 40},
                 {"species": "American Robin", "start": 40, "end": 60}])
    return np.clip(audio, -1, 1), overlays


class Probe:
    def __init__(self, audio, overlays, executor):
        self.audio, self.overlays, self.executor = audio, overlays, executor
        self.ring = np.zeros(30 * SR, dtype=np.float32)
        self.position = 0
        self.stream_t = 0.0
        self.t0 = None
        self.ws = None
        self.events = []
        self.detections = []
        self.windows = []
        self.checklist = []
        self.seen = set()
        self.notifications = []
        self.pending = []
        self.responses = {}
        self.in_progress = False
        self.speaking = False
        self.request_context = None
        self.last_stop = None
        self.stops_answered = set()
        self.vad = []
        self.lags = []
        self.send_lags = []
        self.queue = asyncio.Queue(maxsize=250)
        self.worker = None
        self.analyzer = None
        self.failure = None

    def now(self):
        return round(time.perf_counter() - self.t0, 4) if self.t0 else 0

    def log(self, event_name, **fields):
        record = {"wall_s": self.now(), "stream_s": round(self.stream_t, 3), **fields}
        self.events.append({"event": event_name, **record})
        evidence(event_name, **record)

    async def send(self, event):
        await self.ws.send(json.dumps(event, ensure_ascii=False))

    def analyze(self, samples):
        t = time.perf_counter()
        rec = RecordingBuffer(self.analyzer, samples, SR, lat=43.66, lon=-79.39,
                              date=datetime(2026, 5, 15), min_conf=0.25)
        rec.analyze()
        return rec.detections, (time.perf_counter() - t) * 1000

    async def bird_window(self, samples, end):
        submitted = time.perf_counter()
        detections, compute = await asyncio.get_running_loop().run_in_executor(self.executor, self.analyze, samples)
        elapsed = (time.perf_counter() - submitted) * 1000
        self.windows.append(compute)
        self.log("bird_window", window_start_s=end - 3, window_end_s=end,
                 compute_ms=round(compute, 2), executor_total_ms=round(elapsed, 2), count=len(detections))
        for d in detections:
            record = {"time": end, "window_start": end - 3, "species": d["common_name"],
                      "scientific_name": d.get("scientific_name"), "confidence": round(float(d["confidence"]), 4),
                      "compute_ms": round(compute, 2), "available_wall_s": self.now()}
            self.detections.append(record)
            self.log("bird_detection", **record)
            if record["confidence"] >= 0.5 and record["species"] not in self.seen:
                self.seen.add(record["species"])
                note = {"species": record["species"], "confidence": record["confidence"],
                        "detected_wall_s": self.now(), "window_end_s": end}
                self.pending.append(note)
                self.notifications.append(note)
                self.log("notify_queued", **note, busy=self.in_progress, speaking=self.speaking)
        await self.flush_notify()

    async def flush_notify(self):
        if self.in_progress or self.speaking or not self.pending:
            return
        note = self.pending.pop(0)
        # Reserve before awaiting network sends, so callbacks cannot inject twice.
        self.in_progress = True
        self.request_context = {"kind": "notify", "note": note}
        await self.send({"type": "conversation.item.create", "item": {"type": "message", "role": "user",
                         "content": [{"type": "input_text", "text": f"[NOTIFY] BirdNET heard {note['species']} (conf {note['confidence']:.2f})"}]}})
        note["injected_wall_s"] = self.now()
        self.log("notify_injected", **note, detection_to_inject_ms=round((self.now() - note["detected_wall_s"]) * 1000, 2))
        await self.send({"type": "response.create"})

    def tool(self, name, args):
        if name == "add_observation":
            count = int(args["count"])
            if count < 1:
                return {"error": "count must be positive"}
            self.checklist.append({"species": args["species"], "count": count})
            return {"ok": True, "checklist": self.checklist.copy()}
        if name == "get_checklist":
            return {"checklist": self.checklist.copy(), "species_count": len({x["species"] for x in self.checklist})}
        if name == "recent_sounds":
            seconds = max(0, min(3600, float(args["seconds"])))
            found = [d for d in self.detections if self.stream_t - seconds <= d["time"] <= self.stream_t]
            return {"stream_time": self.stream_t, "seconds": seconds, "detections": found,
                    "species": sorted({d["species"] for d in found}), "source": "BirdNET, unconfirmed"}
        return {"error": "unknown tool"}

    async def receive(self):
        async for raw in self.ws:
            e = json.loads(raw)
            ty = e.get("type", "")
            rid = e.get("response_id")
            if ty == "error":
                self.log("realtime_error", error=e.get("error"))
                self.in_progress = False
            elif ty.startswith("input_audio_buffer.speech_"):
                start = ty.endswith("started")
                self.speaking = start
                audio_t = e.get("audio_start_ms" if start else "audio_end_ms", self.stream_t * 1000) / 1000
                # A start outside TTS (including VAD's 300 ms pre-roll) is a bird-only candidate.
                bird_only = start and not any(o["start"] - 0.5 <= audio_t <= o["end"] + 0.5 for o in self.overlays)
                if not start:
                    self.last_stop = {"wall": self.now(), "audio": audio_t}
                mark = {"type": ty, "audio_time_s": audio_t, "bird_only_candidate": bird_only}
                self.vad.append(mark)
                self.log("vad", **mark)
            elif ty == "conversation.item.input_audio_transcription.completed":
                self.log("user_transcript", text=e.get("transcript"), item_id=e.get("item_id"))
            elif ty == "conversation.item.input_audio_transcription.failed":
                self.log("transcription_failed", error=e.get("error"))
            elif ty == "response.created":
                rid = e["response"]["id"]
                context = self.request_context or {"kind": "vad", "stop": self.last_stop}
                self.request_context = None
                self.responses[rid] = {"text": "", "first_audio": None, "audio_bytes": 0, **context}
                self.in_progress = True
                self.log("response_created", response_id=rid, kind=context["kind"])
            elif ty in ("response.output_audio_transcript.delta", "response.output_text.delta"):
                if rid in self.responses:
                    self.responses[rid]["text"] += e.get("delta", "")
            elif ty == "response.output_audio.delta" and rid in self.responses:
                r = self.responses[rid]
                r["audio_bytes"] += len(base64.b64decode(e["delta"]))
                if r["first_audio"] is None:
                    r["first_audio"] = self.now()
                    metrics = {}
                    if r["kind"] == "notify":
                        note = r["note"]
                        note["first_audio_wall_s"] = self.now()
                        metrics["detection_to_first_audio_ms"] = round((self.now() - note["detected_wall_s"]) * 1000, 2)
                    elif r.get("stop"):
                        stop = r["stop"]
                        if stop["wall"] not in self.stops_answered:
                            self.stops_answered.add(stop["wall"])
                            metrics["vad_stop_to_first_audio_ms"] = round((self.now() - stop["wall"]) * 1000, 2)
                            relevant = [o for o in self.overlays if o["start"] <= stop["audio"] and o["end"] >= stop["audio"] - 2]
                            if relevant:
                                metrics["tts_active_end_to_first_audio_ms"] = round((self.now() - relevant[-1]["active_end"]) * 1000, 2)
                    self.log("first_audio", response_id=rid, kind=r["kind"], **metrics)
            elif ty == "response.done":
                response = e["response"]
                rid = response["id"]
                r = self.responses.get(rid, {})
                self.in_progress = False
                self.log("response_done", response_id=rid, kind=r.get("kind"), status=response.get("status"),
                         text=r.get("text", ""), audio_bytes=r.get("audio_bytes", 0), status_details=response.get("status_details"))
                calls = [it for it in response.get("output", []) if it.get("type") == "function_call"]
                for call in calls:
                    try:
                        args = json.loads(call.get("arguments") or "{}")
                        result = self.tool(call["name"], args)
                    except (ValueError, KeyError, TypeError) as exc:
                        args, result = {}, {"error": str(exc)}
                    self.log("tool_call", name=call["name"], arguments=args, result=result)
                    await self.send({"type": "conversation.item.create", "item": {"type": "function_call_output",
                                     "call_id": call["call_id"], "output": json.dumps(result, ensure_ascii=False)}})
                if calls:
                    self.in_progress = True
                    self.request_context = {"kind": r.get("kind", "vad"), "stop": r.get("stop")}
                    if r.get("note"):
                        self.request_context["note"] = r["note"]
                    await self.send({"type": "response.create"})
                else:
                    await self.flush_notify()

    async def send_audio(self):
        # Stateful anti-alias filter before decimation; ~0.65 ms group delay.
        taps = firwin(63, 10800, fs=SR)
        state = np.zeros(len(taps) - 1)
        while True:
            frame, end = await self.queue.get()
            try:
                if frame is None:
                    return
                filtered, state = lfilter(taps, [1.0], frame, zi=state)
                pcm = (np.clip(filtered[::2], -1, 1) * 32767).astype("<i2").tobytes()
                await self.send({"type": "input_audio_buffer.append", "audio": base64.b64encode(pcm).decode()})
                self.send_lags.append((self.now() - end) * 1000)
            finally:
                self.queue.task_done()

    async def monitor(self):
        target = time.perf_counter() + 0.02
        while True:
            await asyncio.sleep(max(0, target - time.perf_counter()))
            now = time.perf_counter()
            lag = max(0, now - target) * 1000
            self.lags.append(lag)
            if lag > 50:
                self.log("event_loop_stall", lag_ms=round(lag, 2))
            target = now + 0.02

    async def produce(self):
        next_window = 3.0
        max_queue = 0
        for i in range(0, len(self.audio), FRAME):
            # Deliver chunks at their end times, preserving exactly 40 ms cadence.
            end = (i + FRAME) / SR
            await asyncio.sleep(max(0, self.t0 + end - time.perf_counter()))
            frame = self.audio[i:i + FRAME]
            pos = self.position % len(self.ring)
            n = len(frame)
            first = min(n, len(self.ring) - pos)
            self.ring[pos:pos + first] = frame[:first]
            if first < n:
                self.ring[:n - first] = frame[first:]
            self.position += n
            self.stream_t = end
            await self.queue.put((frame, end))
            max_queue = max(max_queue, self.queue.qsize())
            if end + 1e-7 >= next_window:
                if self.worker is not None and not self.worker.done():
                    self.log("bird_window_skipped", window_end_s=next_window, reason="worker busy")
                else:
                    if self.worker:
                        await self.worker  # propagate worker failures
                    # 1.5 s is not a multiple of 40 ms: slice at the nominal
                    # window boundary, not up to the frame's extra 20 ms.
                    window_end = round(next_window * SR)
                    indexes = np.arange(window_end - 3 * SR, window_end) % len(self.ring)
                    samples = self.ring[indexes].copy()
                    self.worker = asyncio.create_task(self.bird_window(samples, next_window))
                next_window += 1.5
        await self.queue.put((None, DURATION))
        await self.queue.join()
        if self.worker:
            await self.worker
        self.log("stream_complete", frames=len(self.audio) // FRAME, max_audio_queue=max_queue)

    async def run(self):
        loop = asyncio.get_running_loop()
        # Load models and warm up lazy audio/resampling imports off the event loop.
        self.analyzer = await loop.run_in_executor(self.executor, Analyzer)
        _, warm_ms = await loop.run_in_executor(self.executor, self.analyze, self.audio[:3 * SR].copy())
        evidence("birdnet_warmup", compute_ms=round(warm_ms, 2))
        self.ws = await connect({"output_modalities": ["audio"], "tools": TOOLS, "tool_choice": "auto",
            "instructions": "You are a concise bilingual birding voice assistant in Toronto. Keep replies to one brief sentence, matching the user's language. User-confirmed counts go into add_observation; a heard bird is not a confirmed sighting. For what just called use recent_sounds(seconds=10). For how many species we have, call BOTH get_checklist and recent_sounds(seconds=60), report distinct species heard and confirmed checklist species separately. Never invent detections. [NOTIFY] messages are automated BirdNET events: briefly announce the species heard without adding a checklist entry or calling tools.",
            "audio": {"input": {"turn_detection": {"type": "server_vad", "threshold": 0.5,
                        "prefix_padding_ms": 300, "silence_duration_ms": 600},
                        "transcription": {"model": "higgs-stt-3.1"}}}})
        while True:
            e = json.loads(await asyncio.wait_for(self.ws.recv(), 30))
            if e["type"] == "error":
                raise RuntimeError(json.dumps(e.get("error")))
            if e["type"] in ("session.created", "session.updated"):
                evidence("session_ready", acknowledgement=e["type"], model=e.get("session", {}).get("model"), turn_detection=e.get("session", {}).get("audio", {}).get("input", {}).get("turn_detection"), note="session.created accepted; server does not reliably emit session.updated; inspect returned settings")
                break
        self.t0 = time.perf_counter()
        receiver = asyncio.create_task(self.receive())
        sender = asyncio.create_task(self.send_audio())
        monitor = asyncio.create_task(self.monitor())
        producer = asyncio.create_task(self.produce())
        try:
            done, _ = await asyncio.wait([receiver, sender, producer], return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                await task
            if not producer.done():
                await producer
            await sender
            # Allow final in-flight responses/notifications to finish, bounded at 18 s.
            deadline = time.perf_counter() + 18
            while time.perf_counter() < deadline and (self.in_progress or self.pending or self.speaking):
                if receiver.done():
                    await receiver
                    break
                await asyncio.sleep(0.1)
            await asyncio.sleep(1)
            if receiver.done():
                await receiver
        finally:
            for task in (producer, sender, receiver, monitor):
                task.cancel()
            await asyncio.gather(producer, sender, receiver, monitor, return_exceptions=True)
            await self.ws.close()

    def summary(self):
        def stats(values):
            return {"n": len(values), "median": round(float(np.median(values)), 2),
                    "p95": round(float(np.percentile(values, 95)), 2), "max": round(float(np.max(values)), 2)} if values else {}
        firsts = [e for e in self.events if e["event"] == "first_audio"]
        self.log("summary", failure=self.failure, bird_windows=len(self.windows),
                 bird_compute_ms=stats(self.windows), event_loop_lag_ms=stats(self.lags),
                 audio_send_lag_ms=stats(self.send_lags),
                 event_loop_stalls_over_50ms=sum(x > 50 for x in self.lags),
                 vad_starts=sum(x["type"].endswith("started") for x in self.vad),
                 bird_only_vad_candidates=sum(x["bird_only_candidate"] for x in self.vad),
                 unique_birdnet_species=sorted({d["species"] for d in self.detections}),
                 checklist=self.checklist, notifications=self.notifications,
                 vad_stop_to_first_audio_ms=stats([e["vad_stop_to_first_audio_ms"] for e in firsts if "vad_stop_to_first_audio_ms" in e]),
                 skipped_windows=sum(e["event"] == "bird_window_skipped" for e in self.events),
                 pending_notifications=len(self.pending))


async def main():
    with REPORT.open("a") as f:
        f.write("\n## Live evidence — " + datetime.now().isoformat(timespec="seconds") + "\n\n")
    probe = None
    try:
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="birdnet") as executor:
            audio, overlays = await asyncio.get_running_loop().run_in_executor(executor, make_stream)
            probe = Probe(audio, overlays, executor)
            try:
                await asyncio.wait_for(probe.run(), 180)
            except Exception as exc:
                probe.failure = type(exc).__name__ + ": " + str(exc)
                evidence("run_failed", error=probe.failure)
                raise
            finally:
                probe.summary()
    except Exception as exc:
        if probe is None:
            evidence("setup_failed", error=type(exc).__name__ + ": " + str(exc))
        raise


if __name__ == "__main__":
    asyncio.run(main())
