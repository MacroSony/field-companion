#!/usr/bin/env python3
"""Phone PCM fan-out, Higgs Realtime, BirdNET, and an in-memory demo checklist."""
import os
os.environ.setdefault("NUMBA_DISABLE_JIT", "1")

import argparse
import asyncio
import base64
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
import json
import logging
from pathlib import Path
import sys
import time
import uuid

import numpy as np
import requests
from scipy.signal import firwin, lfilter
import websockets
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

MODEL = "higgs-realtime-1.5-preview"
SR = 48000
ROOT = Path(__file__).resolve().parents[1]
LOG = logging.getLogger("field-companion")
GROUPS = ["Aves", "Insecta", "Plantae", "Mammalia", "Fungi", "Reptilia", "Amphibia", "Arachnida", "Mollusca", "Actinopterygii"]


def api_key():
    key = os.environ.get("BOSON_API_KEY", "").strip()
    if not key:
        try:
            key = Path("~/.config/boson/api_key").expanduser().read_text().strip()
        except OSError:
            pass
    if not key:
        raise RuntimeError("Set BOSON_API_KEY or ~/.config/boson/api_key")
    return key


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def inat(path, **params):
    response = requests.get("https://api.inaturalist.org/v1/" + path, params=params,
                            headers={"User-Agent": "field-companion-v0"}, timeout=12)
    response.raise_for_status()
    return response.json()


def taxon_names(t):
    return {"sci": t["name"], "en": t.get("english_common_name") or t["name"],
            "zh": t.get("preferred_common_name") or t.get("english_common_name") or t["name"]}


def resolve(name, group=None):
    # Same autocomplete + iconic-group filtering as voice_checklist_probe.py.
    result = inat("taxa/autocomplete", q=name, locale="zh-CN", per_page=10)
    matches = [t for t in result.get("results", [])
               if group is None or t.get("iconic_taxon_name") == group]
    species = [t for t in matches if t.get("rank") == "species"] or matches
    if not species:
        return None
    return {**taxon_names(species[0]), "group": species[0].get("iconic_taxon_name") or group or "Aves"}


def nearby(lat, lng, group):
    since = (date.today() - timedelta(days=60)).isoformat()
    result = inat("observations/species_counts", lat=lat, lng=lng, radius=15,
                  d1=since, iconic_taxa=group, per_page=60, locale="zh-CN")
    return {"lat": lat, "lng": lng, "radius_km": 15, "since": since,
            "species": [{**taxon_names(r["taxon"]), "n": r["count"],
                         "photo": (r["taxon"].get("default_photo") or {}).get("medium_url", "")}
                        for r in result.get("results", [])]}


def tool_schema(name, description, properties=None, required=()):
    return {"type": "function", "name": name, "description": description,
            "parameters": {"type": "object", "properties": properties or {}, "required": list(required)}}


NAME = {"type": "string"}
COUNT = {"type": "integer", "minimum": 1}
TOOLS = [
    tool_schema("add_observation", "Log a user-confirmed sighting; increases existing count. No spoken acknowledgement.",
                {"name": NAME, "group": {"type": "string", "enum": GROUPS}, "count": COUNT,
                 "notes": {"type": "string"}}, ("name", "group", "count", "notes")),
    tool_schema("remove_observation", "Remove a species. No spoken acknowledgement.", {"name": NAME}, ("name",)),
    tool_schema("set_count", "Correct the TOTAL count of an existing species, rather than adding to it. No speech.",
                {"name": NAME, "count": COUNT}, ("name", "count")),
    tool_schema("get_checklist", "Read the confirmed checklist.") ,
    tool_schema("recent_sounds", "Query timestamped BirdNET detections, not confirmed sightings.",
                {"seconds": {"type": "number", "minimum": 0, "maximum": 3600}}, ("seconds",)),
    tool_schema("nearby_species", "List recent iNaturalist species near GPS; narrow down description-based IDs before asking for confirmation.",
                {"group": {"type": "string", "enum": GROUPS}}, ("group",)),
    tool_schema("ignore", "Speech not addressed to the assistant (chatter, noise): stay silent; call ONLY this tool."),
]
INSTRUCTIONS = """You are a concise hands-free birding/naturalist companion. Reply in the user's language, at most one short sentence.
Never claim to have performed any action without its successful tool call. Checklist state is authoritative.
A sighting report such as '两只北美红雀在灌木丛里' means call add_observation with count=2 and notes about the bushes.
For count corrections call set_count, NOT add_observation. After add/remove/set_count call, say NOTHING: the phone plays an earcon.
For unrelated conversation, bird song, noise or chatter call only ignore() and say NOTHING. Never transcribe bird song as human instructions.
For questions about what just called use recent_sounds; distinguish heard species from confirmed checklist sightings.
Use get_checklist for checklist questions. For unnamed organisms use nearby_species and ask before adding.
[NOTIFY] is an automated unconfirmed BirdNET event: briefly ask whether to add the heard species; never auto-add it.
Quiet mode means continue using logging tools, but do not speak unless the user taps. Never invent detections or claim an ID is certain.
"""

# v0 has one shared, in-memory outing: a reconnecting phone sees the same checklist.
# No phone/session identifier exists in the v0 wire protocol. Restart resets the outing.
ITEMS = []
ITEM_LOCK = asyncio.Lock()
CONNECTIONS = set()


async def broadcast_checklist():
    payload = {"type": "checklist", "items": [dict(row) for row in ITEMS]}
    await asyncio.gather(*(c.phone_json(payload) for c in list(CONNECTIONS)), return_exceptions=True)


def valid_count(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100000:
        raise ValueError("count must be an integer between 1 and 100000")
    return value


class PhoneConnection:
    def __init__(self, phone):
        self.phone = phone
        self.id = uuid.uuid4().hex[:8]
        self.phone_lock = asyncio.Lock()
        self.rt_lock = asyncio.Lock()
        self.rt = None
        self.connected = False
        self.closed = False
        self.mic_on = True
        self.quiet = False
        self.tapped = False
        self.lang = "zh"
        self.lat, self.lng = 43.66, -79.39
        self.active = False
        self.user_speaking = False
        self.response_id = None
        self.cancelled_ids = deque(maxlen=100)
        self.audio_allowed = True
        self.play_until = 0.0
        self.playback_task = None
        self.pending = deque()
        self.history = deque(maxlen=3000)
        self.windows = defaultdict(deque)
        self.seen = set()
        self.notified = set()
        self.names = {}
        self.nearby_cache = {}
        self.ring = np.zeros(30 * SR, dtype=np.float32)
        self.position = 0
        self.next_window = 3 * SR
        self.filter_taps = firwin(63, 10800, fs=SR)
        self.filter_state = np.zeros(62)
        self.decimation_phase = 0
        self.audio_queue = asyncio.Queue(maxsize=100)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="birdnet-" + self.id)
        self.analyzer = None
        self.bird_task = None
        self.tasks = []
        self.transcripts = {}
        self.output_buffers = defaultdict(list)
        self.total_audio_in = 0
        self.total_audio_out = 0

    def log(self, event, **fields):
        LOG.info("%s", json.dumps({"connection": self.id, "event": event, **fields}, ensure_ascii=False))

    async def phone_json(self, event):
        if not self.closed:
            async with self.phone_lock:
                await self.phone.send_json(event)

    async def phone_audio(self, pcm):
        if not self.closed:
            async with self.phone_lock:
                await self.phone.send_bytes(pcm)

    async def state(self):
        await self.phone_json({"type": "state", "listening": self.mic_on,
                               "speaking": time.monotonic() < self.play_until,
                               "model": MODEL, "connected": self.connected})

    async def error(self, message):
        await self.phone_json({"type": "error", "message": message})
        await self.phone_json({"type": "earcon", "kind": "error"})

    async def rt_send(self, event):
        async with self.rt_lock:
            if self.rt is None:
                raise ConnectionError("Realtime unavailable")
            await self.rt.send(json.dumps(event, ensure_ascii=False))

    def session(self):
        return {"model": MODEL, "output_modalities": ["audio"], "instructions": INSTRUCTIONS +
                f"\nUser locale: {self.lang}. GPS: {self.lat}, {self.lng}. Quiet mode: {self.quiet}. " +
                ("Speak Mandarin Chinese, including automated notifications, unless the user speaks another language."
                 if self.lang.startswith("zh") else "Use the user's locale for automated notifications."),
                "tools": TOOLS, "tool_choice": "auto",
                "audio": {"input": {"format": {"type": "audio/pcm", "rate": 24000},
                                    "turn_detection": {"type": "server_vad", "threshold": 0.5,
                                                       "prefix_padding_ms": 300, "silence_duration_ms": 600},
                                    "transcription": {"model": "higgs-stt-3.1"}},
                          "output": {"format": {"type": "audio/pcm", "rate": 24000}}}}

    async def inject(self, text):
        await self.rt_send({"type": "conversation.item.create", "item": {"type": "message", "role": "user",
                            "content": [{"type": "input_text", "text": text}]}})

    async def create_response(self):
        self.active = True  # reserve before awaiting network I/O
        await self.rt_send({"type": "response.create"})

    async def flush_pending(self):
        if not self.connected or self.active or self.user_speaking or not self.pending:
            return
        kind, value = self.pending[0]
        if kind == "notify" and (self.quiet or time.monotonic() < self.play_until):
            return
        self.pending.popleft()
        if kind == "notify" and any(row["sci"] == value["sci"] for row in ITEMS):
            await self.flush_pending()
            return
        self.active = True
        text = value if kind == "text" else f"[NOTIFY] BirdNET heard {value['en']} ({value['zh']})"
        await self.inject(text)
        await self.rt_send({"type": "response.create"})
        self.log("injected", kind=kind, text=text)

    async def realtime_loop(self):
        delay = 1
        while not self.closed:
            try:
                key = api_key()
                async with websockets.connect("wss://api.boson.ai/v1/realtime?model=" + MODEL,
                        additional_headers={"Authorization": "Bearer " + key}, max_size=None,
                        open_timeout=20, ping_interval=20, ping_timeout=20) as ws:
                    self.rt = ws
                    self.active = False
                    self.response_id = None
                    self.transcripts.clear()
                    self.output_buffers.clear()
                    await self.rt_send({"type": "session.update", "session": self.session()})
                    # session.updated is not reliably emitted by Higgs; session.created suffices.
                    event = json.loads(await asyncio.wait_for(ws.recv(), 20))
                    if event.get("type") == "error":
                        raise RuntimeError("Realtime session setup rejected")
                    self.connected = True
                    delay = 1
                    self.log("realtime_connected", model=MODEL, acknowledgement=event.get("type"))
                    summary = "; ".join(f"{r['count']} {r['en']} ({r['zh']})" for r in ITEMS) or "empty"
                    await self.rt_send({"type": "conversation.item.create", "item": {"type": "message", "role": "user",
                                        "content": [{"type": "input_text", "text": "Current confirmed checklist (context only; do not speak): " + summary[:3000]}]}})
                    await self.state()
                    await self.handle_rt(event)
                    await self.flush_pending()
                    async for raw in ws:
                        await self.handle_rt(json.loads(raw))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Exception strings from network clients can contain headers: log type only.
                self.log("realtime_disconnected", error_type=type(exc).__name__, retry_s=delay)
                await self.error("Realtime disconnected; reconnecting.")
            finally:
                self.rt = None
                self.connected = False
                self.active = False
                if not self.closed:
                    self.play_until = 0
                    await self.phone_json({"type": "flush"})
                    await self.state()
            await asyncio.sleep(delay)
            delay = min(15, delay * 2)

    async def playback_finished(self):
        while time.monotonic() < self.play_until:
            await asyncio.sleep(max(0.01, self.play_until - time.monotonic()))
        await self.state()
        await self.flush_pending()

    async def handle_rt(self, event):
        ty = event.get("type", "")
        rid = event.get("response_id", self.response_id)
        output_types = ("response.output_audio.delta", "response.audio.delta",
                        "response.output_audio_transcript.delta", "response.audio_transcript.delta",
                        "response.output_text.delta", "response.output_audio_transcript.done",
                        "response.audio_transcript.done", "response.output_text.done")
        if ty in output_types and not event.get("_deliver"):
            # Higgs sometimes emits a spoken preamble BEFORE its tool call. Wait
            # for response.done to decide whether this is a silent logging turn.
            self.output_buffers[rid].append(event)
            return
        if ty == "error":
            code = (event.get("error") or {}).get("code", "unknown")
            message = str((event.get("error") or {}).get("message", ""))[:500]
            try:
                message = message.replace(api_key(), "[redacted]")
            except RuntimeError:
                pass
            self.log("realtime_error", code=code, message=message)
            if code != "response_not_active" and "cancel" not in str(code):
                self.active = False
                await self.error("Realtime request failed (" + str(code)[:100] + ").")
        elif ty == "input_audio_buffer.speech_started":
            self.user_speaking = True
            if time.monotonic() < self.play_until:
                if self.response_id:
                    self.cancelled_ids.append(self.response_id)
                self.play_until = 0
                await self.phone_json({"type": "flush"})
                await self.rt_send({"type": "response.cancel"})
                self.log("barge_in")
            await self.state()
        elif ty == "input_audio_buffer.speech_stopped":
            self.user_speaking = False
        elif ty == "conversation.item.input_audio_transcription.completed":
            text = event.get("transcript", "")
            await self.phone_json({"type": "transcript", "role": "user", "text": text, "final": True})
            self.log("user_transcript", text=text)
        elif ty == "conversation.item.input_audio_transcription.failed":
            await self.error("Input transcription failed.")
        elif ty == "response.created":
            self.active = True
            self.response_id = event["response"]["id"]
            self.audio_allowed = not self.quiet or self.tapped
        elif ty in ("response.output_audio.delta", "response.audio.delta"):
            if rid in self.cancelled_ids or not self.audio_allowed:
                return
            pcm = base64.b64decode(event["delta"])
            was_speaking = time.monotonic() < self.play_until
            await self.phone_audio(pcm)
            self.total_audio_out += len(pcm)
            self.play_until = max(time.monotonic(), self.play_until) + len(pcm) / 48000
            if not was_speaking:
                await self.state()
            if self.playback_task is None or self.playback_task.done():
                self.playback_task = asyncio.create_task(self.playback_finished())
        elif ty in ("response.output_audio_transcript.delta", "response.audio_transcript.delta", "response.output_text.delta"):
            if rid in self.cancelled_ids or not self.audio_allowed:
                return
            key = (rid, event.get("item_id", ""), "text" if "output_text" in ty else "audio")
            self.transcripts[key] = self.transcripts.get(key, "") + event.get("delta", "")
            await self.phone_json({"type": "transcript", "role": "assistant", "text": self.transcripts[key], "final": False})
        elif ty in ("response.output_audio_transcript.done", "response.audio_transcript.done", "response.output_text.done"):
            if rid not in self.cancelled_ids and self.audio_allowed:
                await self.phone_json({"type": "transcript", "role": "assistant",
                                       "text": event.get("transcript", event.get("text", "")), "final": True})
        elif ty == "response.done":
            response = event["response"]
            calls = [item for item in response.get("output", []) if item.get("type") == "function_call"]
            self.log("response_done", status=response.get("status"), tools=[c["name"] for c in calls])
            buffered = self.output_buffers.pop(response.get("id", rid), [])
            if not calls and response.get("status") == "completed":
                for output in buffered:
                    await self.handle_rt({**output, "_deliver": True})
            # Any tool turn's preamble is discarded; read tools get a separate
            # grounded answer via response.create, mutations get only an earcon.
            # Keep the response reserved while potentially slow tool work runs.
            followup = False
            for call in calls:
                name = call["name"]
                if name == "ignore":
                    self.log("tool", name=name, ok=True)
                    continue  # deliberately NO function output and NO response.create
                try:
                    args = json.loads(call.get("arguments") or "{}")
                    result = await self.tool(name, args)
                except (ValueError, TypeError, KeyError) as exc:
                    result = {"ok": False, "error": str(exc)[:180]}
                except Exception as exc:
                    self.log("tool_failed", name=name, error_type=type(exc).__name__)
                    result = {"ok": False, "error": "External service unavailable; try again"}
                await self.rt_send({"type": "conversation.item.create", "item": {"type": "function_call_output",
                                    "call_id": call["call_id"], "output": json.dumps(result, ensure_ascii=False)}})
                self.log("tool", name=name, ok=result.get("ok", True))
                if name in ("add_observation", "remove_observation", "set_count"):
                    await broadcast_checklist()
                    if result.get("ok"):
                        await self.phone_json({"type": "earcon", "kind": "logged"})
                    else:
                        await self.error(result.get("error", "Checklist change failed"))
                elif name in ("get_checklist", "recent_sounds", "nearby_species"):
                    followup = True
            self.active = False
            if followup:
                await self.create_response()
            else:
                self.tapped = False
                await self.flush_pending()
            await self.state()

    async def find_item(self, name):
        needle = str(name).strip().casefold()
        for row in ITEMS:
            if any(str(row.get(k, "")).casefold() == needle for k in ("id", "sci", "en", "zh")):
                return row["id"]
        taxon = await asyncio.to_thread(resolve, str(name), None)
        if taxon:
            return next((r["id"] for r in ITEMS if r["sci"] == taxon["sci"]), None)
        return None

    async def tool(self, name, args):
        if name == "get_checklist":
            return {"items": [dict(r) for r in ITEMS], "species_count": len(ITEMS)}
        if name == "recent_sounds":
            seconds = float(args.get("seconds", 15))
            if not np.isfinite(seconds):
                raise ValueError("seconds must be finite")
            seconds = max(0, min(3600, seconds))
            cutoff = time.monotonic() - seconds
            return {"seconds": seconds, "detections": [d for t, d in self.history if t >= cutoff],
                    "source": "BirdNET; unconfirmed, NOT checklist sightings"}
        if name == "nearby_species":
            group = args.get("group", "Aves")
            if group not in GROUPS:
                raise ValueError("invalid iconic group")
            key = (group, self.lat, self.lng)
            cached = self.nearby_cache.get(key)
            if not cached or time.monotonic() - cached[0] > 300:
                self.nearby_cache[key] = (time.monotonic(), await asyncio.to_thread(nearby, self.lat, self.lng, group))
            return self.nearby_cache[key][1]
        if name == "add_observation":
            count = valid_count(args.get("count", 1))
            group = args.get("group", "Aves")
            if group not in GROUPS:
                raise ValueError("invalid iconic group")
            taxon = await asyncio.to_thread(resolve, str(args["name"]), group)
            if not taxon:
                return {"ok": False, "error": "No matching taxon in that iconic group"}
            async with ITEM_LOCK:
                row = next((r for r in ITEMS if r["sci"] == taxon["sci"]), None)
                if row:
                    row["count"] += count
                    if args.get("notes"):
                        row["notes"] = "; ".join(filter(None, [row["notes"], str(args["notes"])]))
                else:
                    row = {"id": "o" + uuid.uuid4().hex[:10], **taxon, "count": count,
                           "notes": str(args.get("notes", "")), "first_seen": now_iso(), "source": "voice"}
                    ITEMS.append(row)
                return {"ok": True, "item": dict(row)}
        if name in ("remove_observation", "set_count"):
            count = valid_count(args.get("count")) if name == "set_count" else None
            item_id = await self.find_item(args["name"])
            async with ITEM_LOCK:
                row = next((r for r in ITEMS if r["id"] == item_id), None)
                if row is None:
                    return {"ok": False, "error": "Species is not in the checklist"}
                if name == "remove_observation":
                    ITEMS.remove(row)
                else:
                    row["count"] = count
                return {"ok": True, "item": dict(row)}
        raise ValueError("Unknown tool")

    def analyze(self, samples):
        # All analyzer access stays on this connection's single worker thread.
        from birdnetlib import RecordingBuffer
        from birdnetlib.analyzer import Analyzer
        if self.analyzer is None:
            self.analyzer = Analyzer()
        rec = RecordingBuffer(self.analyzer, samples, SR, lat=self.lat, lon=self.lng,
                              date=datetime.now(), min_conf=0.25)
        rec.analyze()
        return rec.detections

    async def bird_window(self, samples, end):
        try:
            start = time.monotonic()
            detections = await asyncio.get_running_loop().run_in_executor(self.executor, self.analyze, samples)
            self.log("bird_window", stream_s=round(end / SR, 2), count=len(detections),
                     compute_ms=round((time.monotonic() - start) * 1000))
            for d in detections:
                sci = d.get("scientific_name") or d["common_name"]
                if sci not in self.names:
                    try:
                        taxon = await asyncio.to_thread(resolve, sci, "Aves")
                    except Exception:
                        taxon = None
                    self.names[sci] = taxon or {"sci": sci, "en": d["common_name"], "zh": d["common_name"]}
                names = {k: self.names[sci][k] for k in ("sci", "en", "zh")}
                confidence = round(float(d["confidence"]), 4)
                first = confidence >= 0.5 and sci not in self.seen
                if confidence >= 0.5:
                    self.seen.add(sci)
                heard = {"type": "heard", **names, "conf": confidence, "t": now_iso(), "new": first}
                self.history.append((time.monotonic(), {k: v for k, v in heard.items() if k != "type"}))
                await self.phone_json(heard)
                self.log("bird_detection", **names, conf=confidence, stream_s=round(end / SR, 2))
                if first:
                    await self.phone_json({"type": "earcon", "kind": "heard"})
                hits = self.windows[sci]
                while hits and end - hits[0] > 10 * SR:
                    hits.popleft()
                if confidence >= 0.5 and (not hits or hits[-1] != end):
                    hits.append(end)
                if len(hits) >= 2 and sci not in self.notified and not any(r["sci"] == sci for r in ITEMS):
                    self.notified.add(sci)
                    self.pending.append(("notify", names))
                    self.log("notify_queued", sci=sci, windows=len(hits))
            await self.flush_pending()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.log("birdnet_failed", error_type=type(exc).__name__)
            await self.error("BirdNET analysis unavailable; voice and checklist still work.")

    async def ingest_audio(self, raw):
        if not self.mic_on or not raw:
            return
        if len(raw) % 2 or len(raw) > SR * 2:
            raise ValueError("Expected mono 48 kHz PCM16 frames of at most one second")
        frame = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768
        n = len(frame)
        pos = self.position % len(self.ring)
        first = min(n, len(self.ring) - pos)
        self.ring[pos:pos + first] = frame[:first]
        if first < n:
            self.ring[:n - first] = frame[first:]
        self.position += n
        self.total_audio_in += len(raw)
        # Stateful 63-tap anti-alias filter, ~0.65 ms delay; phase survives odd-length packets.
        filtered, self.filter_state = lfilter(self.filter_taps, [1.0], frame, zi=self.filter_state)
        pcm = (np.clip(filtered[self.decimation_phase::2], -1, 1) * 32767).astype("<i2").tobytes()
        self.decimation_phase = (self.decimation_phase - n) % 2
        if self.connected:
            if self.audio_queue.full():
                self.audio_queue.get_nowait()  # bounded latency rather than stale audio after a network stall
                self.log("audio_queue_drop")
            self.audio_queue.put_nowait(pcm)
        while self.position >= self.next_window:
            if self.bird_task is None or self.bird_task.done():
                end = self.next_window
                samples = self.ring[np.arange(end - 3 * SR, end) % len(self.ring)].copy()
                self.bird_task = asyncio.create_task(self.bird_window(samples, end))
            else:
                self.log("bird_window_skipped", reason="worker busy")
            self.next_window += int(1.5 * SR)

    async def audio_sender(self):
        while True:
            pcm = await self.audio_queue.get()
            try:
                if self.connected:
                    await self.rt_send({"type": "input_audio_buffer.append", "audio": base64.b64encode(pcm).decode()})
            except Exception as exc:
                self.log("audio_send_failed", error_type=type(exc).__name__)
            finally:
                self.audio_queue.task_done()

    async def handle_phone(self, event):
        ty = event.get("type")
        if ty == "hello":
            if event.get("lat") is not None and event.get("lng") is not None:
                lat, lng = float(event["lat"]), float(event["lng"])
                if not (-90 <= lat <= 90 and -180 <= lng <= 180):
                    raise ValueError("Invalid GPS coordinates")
                self.lat, self.lng = lat, lng
            self.lang = str(event.get("lang", "zh"))[:32]
            await self.state()
            await self.phone_json({"type": "checklist", "items": [dict(r) for r in ITEMS]})
        elif ty == "mic":
            if not isinstance(event.get("on"), bool):
                raise ValueError("mic.on must be boolean")
            self.mic_on = event["on"]
            await self.state()
        elif ty == "mode":
            if not isinstance(event.get("quiet"), bool):
                raise ValueError("mode.quiet must be boolean")
            self.quiet = event["quiet"]
            if self.quiet:
                self.play_until = 0
                self.audio_allowed = False
                await self.phone_json({"type": "flush"})
            if self.connected:
                await self.rt_send({"type": "session.update", "session": {"instructions": self.session()["instructions"]}})
            await self.state()
            await self.flush_pending()
        elif ty == "tap":
            self.tapped = True
            if self.connected:
                await self.inject("[TAP] The next user utterance is addressed to you; you may speak in quiet mode.")
        elif ty == "text":
            text = event.get("text", "")
            if not isinstance(text, str) or not text.strip() or len(text) > 8000:
                raise ValueError("text must contain 1–8000 characters")
            self.pending.appendleft(("text", text))
            await self.flush_pending()
        elif ty in ("edit", "delete"):
            count = valid_count(event.get("count")) if ty == "edit" else None
            async with ITEM_LOCK:
                row = next((r for r in ITEMS if r["id"] == event.get("id")), None)
                if not row:
                    raise ValueError("Unknown observation id")
                if ty == "edit":
                    row["count"] = count
                else:
                    ITEMS.remove(row)
            await broadcast_checklist()
            await self.phone_json({"type": "earcon", "kind": "logged"})
            if self.connected:
                summary = json.dumps(ITEMS, ensure_ascii=False)
                await self.rt_send({"type": "conversation.item.create", "item": {"type": "message", "role": "user",
                                    "content": [{"type": "input_text", "text": "Manual checklist update (context only): " + summary[:6000]}]}})
        else:
            raise ValueError("Unknown message type")

    async def run(self):
        await self.phone.accept()
        CONNECTIONS.add(self)
        self.log("phone_connected")
        try:
            hello = await asyncio.wait_for(self.phone.receive_json(), 20)
            if not isinstance(hello, dict) or hello.get("type") != "hello":
                await self.error("Send hello first")
                await self.phone.close(code=1008)
                return
            await self.handle_phone(hello)
            self.tasks = [asyncio.create_task(self.realtime_loop()), asyncio.create_task(self.audio_sender())]
            while True:
                packet = await self.phone.receive()
                if packet["type"] == "websocket.disconnect":
                    break
                try:
                    if packet.get("bytes") is not None:
                        await self.ingest_audio(packet["bytes"])
                    elif packet.get("text") is not None:
                        event = json.loads(packet["text"])
                        if not isinstance(event, dict):
                            raise ValueError("JSON message must be an object")
                        await self.handle_phone(event)
                except (ValueError, KeyError, TypeError) as exc:
                    await self.error(str(exc)[:180])
                except Exception as exc:
                    self.log("phone_message_failed", error_type=type(exc).__name__)
                    await self.error("Request could not be completed")
        except (WebSocketDisconnect, asyncio.TimeoutError):
            pass
        finally:
            self.closed = True
            CONNECTIONS.discard(self)
            tasks = self.tasks + [t for t in (self.bird_task, self.playback_task) if t is not None]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.executor.shutdown(wait=False, cancel_futures=True)
            self.log("phone_disconnected", input_bytes=self.total_audio_in, output_bytes=self.total_audio_out)


app = FastAPI(title="Field Companion v0")


@app.websocket("/ws")
async def phone_socket(ws: WebSocket):
    await PhoneConnection(ws).run()


# Mount after the websocket route so /ws is never shadowed by the static app.
if (ROOT / "web").is_dir():
    app.mount("/", StaticFiles(directory=str(ROOT / "web"), html=True), name="web")
else:
    @app.get("/", response_class=HTMLResponse)
    async def placeholder():
        return "<h1>Field Companion</h1><p>Backend is running. Connect the phone to /ws; web/ is not installed yet.</p>"


if __name__ == "__main__":
    import uvicorn
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(asctime)s %(levelname)s %(message)s")
    # Never enable websocket HTTP/header debug logging: it can expose Authorization.
    logging.getLogger("websockets").setLevel(logging.WARNING)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
