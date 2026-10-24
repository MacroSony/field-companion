#!/usr/bin/env python3
"""Phone PCM, Higgs, BirdNET and in-memory Detection/Observation/Checklist."""
import os
os.environ.setdefault("NUMBA_DISABLE_JIT", "1")

import argparse
import asyncio
import base64
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from dataclasses import dataclass, field, asdict
import math
import io
from difflib import SequenceMatcher
import json
import logging
import re
from pathlib import Path
import sys
import time
import uuid

import numpy as np
import requests
from scipy.signal import firwin, lfilter
import websockets
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from expert import NaturalistExpert, revision as expert_revision

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
    en = t.get("english_common_name")
    if not en:
        result = inat("taxa/" + str(t["id"]), locale="en").get("results", [])
        english = result[0] if result else {}
        en = english.get("english_common_name") or english.get("preferred_common_name") or t["name"]
    photo = t.get("default_photo") or {}
    return {"sci": t["name"], "en": en, "zh": t.get("preferred_common_name") or en,
            "photo": photo.get("square_url") or photo.get("medium_url") or ""}


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
            "species": [{**taxon_names(r["taxon"]), "n": r["count"]}
                        for r in result.get("results", [])]}


@dataclass
class Detection:
    id: str
    kind: str
    t: str
    candidates: list = field(default_factory=list)
    thumb: str | None = None
    text: str | None = None


@dataclass
class Observation:
    id: str
    status: str = "open"
    detections: list = field(default_factory=list)
    fused: list = field(default_factory=list)
    uncertain: bool = True
    hint: str = ""
    count: int | None = None
    notes: str = ""
    t_start: str = field(default_factory=now_iso)
    lat: float = 43.66
    lng: float = -79.39
    group: str = "Aves"
    touched: float = field(default_factory=time.monotonic, repr=False)
    awaiting_answer: bool = field(default=False, repr=False)

    def payload(self):
        return {k: v for k, v in asdict(self).items() if k not in ("touched", "awaiting_answer")}


def fuse(detections, allowed=None, priors=None):
    """Log-linear fusion; missing candidates get .01, resolved descriptions a 2x boost.

    Only species actually seen in evidence are scored. Empty allowed means the
    range service was unavailable; fail open rather than erase all evidence.
    """
    priors = priors or {}
    names = {c["sci"]: c for d in detections for c in d.candidates}
    species = sorted(s for s in names if not allowed or s in allowed)
    scores = {}
    for sci in species:
        score = math.log(max(0, priors.get(sci, 0)) + 1)
        for d in detections:
            p = next((float(c["p"]) for c in d.candidates if c["sci"] == sci), None)
            if d.kind == "description":
                if p is not None:
                    score += math.log(2) * max(0, min(1, p))
            else:
                score += (0.5 if d.kind == "sound" else 1) * math.log(max(1e-6, min(1, p if p is not None else .01)))
        scores[sci] = score
    if not scores:
        return [], True
    peak = max(scores.values())
    weights = {s: math.exp(v - peak) for s, v in scores.items()}
    total = sum(weights.values())
    fused = [{"sci": sci, "en": names[sci].get("en", sci),
              "photo": names[sci].get("photo", ""), "p": weights[sci] / total}
             for sci in sorted(weights, key=lambda s: (-weights[s], s))]
    margin = fused[0]["p"] - (fused[1]["p"] if len(fused) > 1 else 0)
    return fused, fused[0]["p"] < .5 or margin < .2


def classify_frame(jpeg, crop, candidates):
    try:
        response = requests.post(VISION_URL + "/classify", json={
            "jpeg_b64": base64.b64encode(jpeg).decode(), "crop": crop,
            "candidates": candidates or None, "k": 5}, timeout=30)
        response.raise_for_status()
        result = response.json()
        for c in result["candidates"]:
            if not isinstance(c["sci"], str) or not math.isfinite(float(c["p"])) or not 0 <= float(c["p"]) <= 1:
                raise ValueError("invalid candidate")
        return result
    except (requests.RequestException, ValueError, KeyError, TypeError):
        raise HTTPException(503, "Vision service unavailable or invalid response (expected " + VISION_URL + "/classify)")


def tool_schema(name, description, properties=None, required=()):
    return {"type": "function", "name": name, "description": description,
            "parameters": {"type": "object", "properties": properties or {}, "required": list(required)}}


VISION_URL = os.environ.get("VISION_URL", "http://127.0.0.1:8795")


def _web_build():
    try:
        m = re.search(r"CLIENT_BUILD = '([^']+)'", (Path(__file__).resolve().parent.parent / "web" / "app.js").read_text())
        return m.group(1) if m else "unknown"
    except OSError:
        return "unknown"


SERVER_BUILD = _web_build()

NAME = {"type": "string"}
COUNT = {"type": "integer", "minimum": 1}
SUGGEST_DEBOUNCE = 3.5
SUGGEST_INTERVAL = 25
SUGGEST_STALE = 60
SUGGEST_TTL = 120
SPECIES = {"type": "array", "items": NAME}
OBSERVATION = {"type": "object", "properties": {
    "name": NAME, "group": {"type": "string", "enum": GROUPS},
    "count": COUNT, "notes": NAME}, "required": ["name"]}
WRITE_TOOLS = ("add_observation", "add_observations", "accept_suggestions",
               "dismiss_suggestions", "set_count", "remove_observation", "confirm_observation")
TOOLS = [
    tool_schema("look", "Identify what the user is looking at ('what is that?'); request camera frames. Stay silent until frames arrive. Set group for nonbirds.", {"hint": NAME, "group": {"type": "string", "enum": GROUPS}}),
    tool_schema("get_observation", "Read focus evidence for pronouns and follow-up naturalist questions."),
    tool_schema("confirm_observation", "Add it / log it / two of them: confirm the focus, not a new named sighting. No speech.", {"count": COUNT, "notes": NAME}),
    tool_schema("discard_observation", "Discard the focus; do not log it."),
    tool_schema("new_observation", "Start a different organism as a new focus.", {"group": {"type": "string", "enum": GROUPS}}),
    tool_schema("describe", "Attach a user description to the focus; resolve only explicit species names.", {"text": NAME, "name": NAME}, ("text",)),
    tool_schema("ask_expert", "Reason about the current focus: candidate comparisons, 'could it be X?', conflicting evidence, diet, habitat or behaviour. Read-only; never log.", {"question": NAME}, ("question",)),
    tool_schema("accept_suggestions", 'Accept pending suggestions only on explicit yes/yeah/sure or an intelligible add command. Never accept on lone Ah/Okay/Uh/Oh/Mm/Mm-hmm, questions, or a notification. Silent.',
                {"species": SPECIES, "counts": {"type": "object", "additionalProperties": COUNT}}),
    tool_schema("dismiss_suggestions", "Decline pending suggestions. No = all; optional subset. Silent.", {"species": SPECIES}),
    tool_schema("add_observations", "Log only explicit organism sighting reports in ONE action; increase existing counts. Never log questions, greetings, or mic checks. No speech.",
                {"items": {"type": "array", "items": OBSERVATION, "minItems": 1}}, ("items",)),
    tool_schema("add_observation", "Log only an explicit organism sighting report; increase existing count. Never log questions, greetings, or mic checks. No speech.",
                {"name": NAME, "group": {"type": "string", "enum": GROUPS}, "count": COUNT,
                 "notes": {"type": "string"}}, ("name", "group", "count", "notes")),
    tool_schema("remove_observation", "Remove a species, or clear the checklist with all=true ONLY when the user explicitly says all/everything/clear. Silent.",
                {"name": NAME, "all": {"type": "boolean"}}),
    tool_schema("set_count", "Correct the TOTAL count of an existing species, rather than adding to it. No speech.",
                {"name": NAME, "count": COUNT}, ("name", "count")),
    tool_schema("get_checklist", "Fetch confirmed logged sightings/counts, not sounds heard.") ,
    tool_schema("recent_sounds", "Fetch recent sounds heard: unconfirmed BirdNET detections.",
                {"seconds": {"type": "number", "minimum": 0, "maximum": 3600}}, ("seconds",)),
    tool_schema("nearby_species", "List recent iNaturalist species near GPS; narrow down description-based IDs before asking for confirmation.",
                {"group": {"type": "string", "enum": GROUPS}}, ("group",)),
    tool_schema("speak", "Request speech for general questions, greetings, or mic checks ('Can you hear me?'); not for questions needing checklist or recent-sound data. No answer during routing. Never for lone fillers/acknowledgements (Ah/Okay/Uh/Oh/Mm/Mm-hmm): ignore those.", {"reason": NAME}, ("reason",)),
    tool_schema("ignore", 'Silently ignore chatter, unintelligible fragments, incomplete commands, and lone fillers/acknowledgements (Ah, Okay, Uh, Oh, Mm, Mm-hmm). These fillers MUST be ignored even with pending suggestions or an unfinished prior request; never accept or dismiss on them.'),
]
INSTRUCTIONS = """You are a concise hands-free birding/naturalist companion with an open microphone.
Reply in English by default; the user may speak Chinese: answer in the language of the question.
Classify ONLY the latest user turn. Ordinary off-topic remarks/questions to a companion,
self-talk, phone calls, noise and pet-dog chatter are NOT addressed to you: call ignore.
Nature sighting reports are addressed even without a wake word: call add_observation.
Multiple species in one report: call add_observations with all items in ONE call.
When pending BirdNET suggestions exist: yes / yes both / add them all -> accept_suggestions
with omitted species (all pending); just the cardinal -> accept_suggestions with that species.
No / don't add them -> dismiss_suggestions. These actions are silent, not speak.
Only accept explicit user confirmation, never a BirdNET notification itself.
For total count corrections use set_count; for deletion use remove_observation.
Never claim to have logged anything without a successful tool call. Logging has an earcon,
not speech. Never give delayed logging acknowledgements, including "I've logged X".
For "what's that?", "look again", or visual identification call look, NOT speak.
For "add it", "two of them", or "log that" call confirm_observation with the count.
Pronouns refer to the focus. For species comparisons ('could it be a Cooper's
Hawk?'), uncertain identification, diet ('what does it eat?'), habitat or behaviour
call ask_expert with the complete latest question, NOT speak or get_observation.
get_observation only reads evidence; it does not supply species facts.
For a different organism call new_observation; discard unwanted IDs with discard_observation.
Questions about birds heard, sounds, calls, or 'anything else' use recent_sounds(seconds=15). Only explicit questions about logged sightings/counts use get_checklist.
Greetings, mic checks and general questions addressed to you use speak; sound/checklist questions use their read tools.
For local ID candidates use nearby_species.
Filler-only speech and unintelligible fragments (including "As for you, for me.") use ignore.
Questions such as "Did you hear them?" never approve pending suggestions: use recent_sounds.
Never invent sightings or identify from insufficient evidence; request camera evidence with look or ask for a description.
"""
ROUTING = INSTRUCTIONS + """
ROUTING PHASE ONLY: select exactly ONE function call with NO text or audio.
Chatter -> ignore; sighting -> add_observation; addressed question -> speak or a read tool.
Earlier tool outputs are already handled. Do not invent sightings from questions.
[TAP] means the next utterance is addressed to you.
"""
ROUTING = 'HIGHEST-PRIORITY ROUTING RULE: A turn consisting only of ah, okay, uh, oh, mm, or mm-hmm is ignore, even immediately after a pending-suggestion question or an unfinished earlier request. These fillers are NOT approval or dismissal. Yes/yeah/sure or an intelligible add command can approve. Apply this before choosing any other tool.' + "\n" + ROUTING
ANSWER = INSTRUCTIONS + """
ANSWER PHASE: answer ONLY the latest addressed question in ONE short sentence. No tools.
Use supplied tool results and checklist. Never acknowledge any earlier log or chatter;
no "I've logged X" acknowledgements in answers. Reply in English by default, but in the
language of the question (including Chinese). [NOTIFY] is an automated, unconfirmed BirdNET
event: briefly ask whether to add the heard species, in English; NEVER claim it is logged.
[VISION] contains the current focus top-3 and uncertain flag. If certain say "Looks like a
<species>"; if uncertain say "Not sure — <top two>; can you zoom in?". Use hint when
present to request a discriminating close-up. If top3 is empty, say you cannot identify
it yet and ask for a closer frame. Never claim a vision ID is logged.
[EXPERT] or an ask_expert tool result contains final wording in answer. Speak
that answer exactly, preserving uncertainty and questions; do not add facts,
reinterpret the ranking, promise a capture, or acknowledge logging. For an
ask_expert error without answer, ask the user to look at an organism first.
"""
SESSIONS = {}
SESSION_TTL = 600
FOCUS_TTL = 120
ALLOWLIST_DIR = Path("/data/build/tmp")


def scientific_species(name):
    return bool(re.fullmatch(r"[A-Z][a-z]+ [a-z][a-z-]+", name or "")) and not (name or "").startswith("Human ")


def eligible_detections(detections, allowed):
    return [{**d, "unusual": False} for d in detections
            if scientific_species(d.get("scientific_name"))
            and d["scientific_name"] in allowed
            and not re.match(r"^(human|dog|engine|siren|gun|power tools|fireworks|noise|environmental)\b",
                             d.get("common_name", ""), re.I)
            and float(d["confidence"]) >= .25]


def valid_count(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100000:
        raise ValueError("count must be an integer between 1 and 100000")
    return value


class TurnItems(set):
    failed = False


class TurnText(str):
    """Transcript plus any already-committed audio item (never a newer turn)."""
    def __new__(cls, text, item_ids=()):
        obj = super().__new__(cls, text)
        obj.item_ids = set(item_ids)
        return obj


class Session:
    def __init__(self, session_id):
        self.id = session_id
        self.clients = set()
        self.mic = None
        self.observations = {}
        self.focus = None
        self.frames = {}
        self.sound_windows = deque(maxlen=30)
        self.item_lock = asyncio.Lock()
        self.expiry_task = None
        self.update_lock = asyncio.Lock()
        self.update_waiter = None
        self.response_waiter = None
        self.awaited_response_id = None
        self.conversation_ids = deque()
        self.history_lock = asyncio.Lock()
        self.turn_items = None
        self.deleted_item_ids = deque(maxlen=500)
        self.item_owners = {}
        self.response_owners = {}
        self.tools_executed = False
        self.turn_task = None
        self.turn_in_progress = False
        self.epoch = 0
        self.phase = "route"
        self.work_ready = asyncio.Event()
        self.tap_at = 0.0
        self.speech_stopped_at = None
        self.turn_stopped_at = None
        self.first_audio = False
        self.rt_lock = asyncio.Lock()
        self.rt = None
        self.connected = False
        self.closed = False
        self.mic_on = True
        self.quiet = False
        self.lang = "en"
        self.lat, self.lng = 43.66, -79.39
        self.active = False
        self.user_speaking = False
        self.awaiting_transcript = False
        self.response_id = None
        self.cancelled_ids = deque(maxlen=100)
        self.audio_allowed = True
        self.play_until = 0.0
        self.echo_estimate = 0.0
        self.spoken = deque(maxlen=3)
        self.playback_task = None
        self.pending = deque()
        self.history = deque(maxlen=3000)
        self.windows = defaultdict(deque)
        self.seen = set()
        self.notified = set()
        self.suggestions = {}  # insertion ordered; monotonic times stay server-side
        self.last_proactive = float("-inf")
        self.proactive_turn = False
        self.names = {}
        self.nearby_cache = {}
        self.ring = np.zeros(30 * SR, dtype=np.float32)
        self.echo_ring = np.zeros(len(self.ring), dtype=bool)
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
        self.total_audio_in = 0
        self.total_audio_out = 0
        self.expert = NaturalistExpert()
        self.input_revision = 0

    async def ask_expert(self, question, obs):
        snapshot = obs.payload()
        version = expert_revision(snapshot)
        input_revision = self.input_revision
        result = await self.expert.answer(question, snapshot)
        await self.expire_focus()
        if (self.closed or self.focus != obs.id or obs.status == "discarded"
                or self.input_revision != input_revision
                or expert_revision(obs.payload()) != version):
            self.log("expert_stale", observation=obs.id)
            return {"ok": False, "status": "stale", "suppress_answer": True}
        if result.get("action") == "request_more_evidence" and result.get("hint") and obs.status == "open":
            obs.hint = result["hint"]
            version = expert_revision(obs.payload())
            await self.broadcast_observation(obs)
        if (self.focus != obs.id or self.input_revision != input_revision
                or expert_revision(obs.payload()) != version):
            return {"ok": False, "status": "stale", "suppress_answer": True}
        result["context"] = {"observation_id": obs.id, "revision": version, "input_revision": input_revision}
        self.log("expert_done", status=result.get("status"), action=result.get("action"))
        return result

    def expert_result_current(self, result):
        context = result.get("context", {})
        obs = self.observations.get(context.get("observation_id"))
        return (obs is not None and self.focus == obs.id and obs.status != "discarded"
                and self.input_revision == context.get("input_revision")
                and expert_revision(obs.payload()) == context.get("revision"))

    async def answer_vision(self, observation_id):
        obs = self.observations.get(observation_id)
        input_revision = self.input_revision
        if not obs or obs.status != "open" or self.focus != obs.id or not self.may_speak():
            return
        if obs.uncertain and self.expert.available:
            # Give a short capture burst time to land; snapshot validation below
            # also rejects frames arriving during the model call.
            await asyncio.sleep(1)
            if (self.focus != obs.id or obs.status != "open"
                    or self.input_revision != input_revision or not self.may_speak()):
                return
            question = "根据现有证据，这是什么？不确定时告诉我还需要拍什么。" if self.lang.startswith("zh") else (
                "What is this organism, based on the evidence? If uncertain, what should I capture next?")
            result = await self.ask_expert(question, obs)
            if result.get("suppress_answer") and self.input_revision != input_revision:
                return
            # An unavailable expert must not prevent the existing BioCLIP answer.
            if result.get("ok"):
                if self.may_speak():
                    await self.inject("[EXPERT] " + json.dumps(result, ensure_ascii=False))
                    if self.expert_result_current(result):
                        await self.generate("answer")
                return
        if (self.focus == obs.id and obs.status == "open" and self.may_speak()
                and self.input_revision == input_revision):
            await self.inject("[VISION] " + json.dumps({"top3": obs.fused[:3], "uncertain": obs.uncertain, "hint": obs.hint}))
            await self.generate("answer")

    @property
    def items(self):
        rows = {}
        for obs in self.observations.values():
            if obs.status != "confirmed" or not obs.fused:
                continue
            top = obs.fused[0]
            sci = top["sci"]
            if sci not in rows:
                rows[sci] = {"id": obs.id, "sci": sci, "en": top["en"], "photo": top.get("photo", ""),
                             "count": 0, "notes": "", "first_seen": obs.t_start, "group": obs.group,
                             "lat": obs.lat, "lng": obs.lng,
                             "source": "photo" if any(d.kind == "image" for d in obs.detections) else (
                                 "voice" if any(d.kind == "description" for d in obs.detections) else "birdnet")}
            rows[sci]["count"] += obs.count or 1
            rows[sci]["notes"] = "; ".join(filter(None, [rows[sci]["notes"], obs.notes]))
        return list(rows.values())

    async def broadcast_observation(self, obs):
        await self.phone_json({"type": "observation", **obs.payload(), "focus": self.focus == obs.id})

    async def expire_focus(self):
        obs = self.observations.get(self.focus)
        if obs and time.monotonic() - obs.touched >= FOCUS_TTL:
            self.focus = None
            if obs.status == "open":
                obs.status = "discarded"
            obs.awaiting_answer = False
            await self.broadcast_observation(obs)

    async def focus_watch(self):
        while not self.closed:
            await asyncio.sleep(1)
            await self.expire_focus()

    async def new_focus(self, group="Aves"):
        old = self.observations.get(self.focus)
        if old and old.status == "open":
            old.status = "discarded"
            old.awaiting_answer = False
        obs = Observation("ob" + uuid.uuid4().hex[:10], lat=self.lat, lng=self.lng, group=group)
        self.observations[obs.id] = obs
        self.focus = obs.id
        if old:
            await self.broadcast_observation(old)
        await self.broadcast_observation(obs)
        return obs

    async def current_focus(self, create=False):
        await self.expire_focus()
        obs = self.observations.get(self.focus)
        if create and (not obs or obs.status != "open"):
            obs = await self.new_focus()
        if obs:
            obs.touched = time.monotonic()
        return obs

    async def refusion(self, obs, touch=True):
        allowed = await asyncio.get_running_loop().run_in_executor(self.executor, self.allowed_species) if obs.group == "Aves" else None
        if self.observations.get(obs.id) is not obs:
            return  # reset/remove-all while the range lookup was in flight
        priors = getattr(self, "_prior_counts", {})
        obs.fused, obs.uncertain = fuse(obs.detections, allowed, priors)
        if touch:
            obs.touched = time.monotonic()
        await self.broadcast_observation(obs)

    async def request_look(self, hint="", group=None):
        if group is not None and group not in GROUPS:
            raise ValueError("invalid iconic group")
        obs = await self.current_focus(create=True)
        if group is not None:
            obs.group = group
        obs.hint = str(hint or obs.hint)[:500]
        obs.awaiting_answer = True
        known = {d.id for d in obs.detections}
        for stamp, detection in self.sound_windows:
            if abs(time.monotonic() - stamp) <= 10 and detection.id not in known:
                obs.detections.append(detection)
        await self.refusion(obs)
        if self.mic:
            await self.mic.send_json({"type": "capture", "obs": obs.id, "n": 3, "hint": obs.hint})
        return {"ok": True, "observation": obs.payload(), "waiting_for_frames": True}

    async def change_checklist(self, item_id, count=None):
        row = next((r for r in self.items if r["id"] == item_id), None)
        if not row:
            raise ValueError("Unknown checklist id")
        members = [o for o in self.observations.values() if o.status == "confirmed" and o.fused and o.fused[0]["sci"] == row["sci"]]
        # A total edit consolidates the species into the representative observation.
        for i, obs in enumerate(members):
            if count is None or i:
                obs.status = "discarded"
                obs.awaiting_answer = False
                if self.focus == obs.id:
                    self.focus = None
            else:
                obs.count = count
            await self.broadcast_observation(obs)
        await self.broadcast_checklist()
        return row

    def log(self, event, **fields):
        LOG.info("%s", json.dumps({"connection": self.id, "event": event, **fields}, ensure_ascii=False))

    async def phone_json(self, event):
        await asyncio.gather(*(c.send_json(event) for c in list(self.clients)), return_exceptions=True)

    async def phone_audio(self, pcm):
        await asyncio.gather(*(c.send_audio(pcm) for c in list(self.clients)
                               if c.role == "mic" or c.audio), return_exceptions=True)

    @property
    def speaker_mode(self):
        mic = self.mic or next((c for c in self.clients if c.role == "mic"), None)
        return mic is None or not mic.earphones

    @property
    def echo_guard(self):
        return self.speaker_mode or any(c.role == "display" and c.audio for c in self.clients)

    def playback_clients(self):
        return [c for c in self.clients if c.role == "mic" or c.audio]

    def echo_deadline(self):
        clients = self.playback_clients()
        # A start without an end keeps the gate closed, including suspended WebAudio.
        if any(c.playback_active for c in clients):
            return float("inf")
        estimate = max(self.echo_estimate, self.play_until)
        return max((max(estimate, c.playback_end) + (.5 if c.playback_end else .8)
                    for c in clients), default=estimate + .8)

    @staticmethod
    def normalized_words(text):
        return "".join(c.lower() if c.isalnum() else " " for c in text).split()

    @staticmethod
    def filler_only(text):
        words = re.sub(r"[^\w\s]", "", str(text).lower()).split()
        return bool(words) and all(w in {"ah", "uh", "um", "oh", "hmm", "mm", "mmhmm", "mhm", "er", "erm"} for w in words)

    def remember_spoken(self, key, text):
        if not text.strip():
            return
        for row in list(self.spoken):
            if row[0] == key:
                self.spoken.remove(row)
                break
        self.spoken.append((key, text, time.monotonic()))

    def transcript_is_echo(self, text):
        words = self.normalized_words(text)
        if not words:
            return False
        now = time.monotonic()
        recent = [line for _, line, at in self.spoken
                  if now - max(at, self.play_until, self.echo_estimate,
                               max((c.playback_end for c in self.clients), default=0)) <= 10]
        # Echo = a long CONTIGUOUS run of the assistant's own words. Bag-of-words overlap
        # misfired on answers like "Yes, add them" to "...add them?" or "Cardinal." (10/23).
        if len(words) < 3:
            return False
        for line in recent + ([" ".join(recent)] if len(recent) > 1 else []):
            other = self.normalized_words(line)
            m = SequenceMatcher(None, words, other, autojunk=False).find_longest_match(0, len(words), 0, len(other))
            if m.size >= 3 and m.size >= .7 * len(words):
                return True
        return False

    def state_payload(self):
        return {"type": "state", "listening": self.mic_on,
                "speaking": time.monotonic() < self.play_until,
                "model": MODEL, "connected": self.connected, "echo_guard": self.echo_guard,
                "session": self.id, "server_build": SERVER_BUILD}

    async def state(self):
        await self.phone_json(self.state_payload())

    async def broadcast_checklist(self):
        await self.phone_json({"type": "checklist", "items": [dict(r) for r in self.items]})

    def may_speak(self):
        return not self.quiet or time.monotonic() - self.tap_at < 10

    async def error(self, message):
        await self.phone_json({"type": "error", "message": message})
        await self.phone_json({"type": "earcon", "kind": "error"})

    async def rt_send(self, event):
        async with self.rt_lock:
            if self.rt is None:
                raise ConnectionError("Realtime unavailable")
            await self.rt.send(json.dumps(event, ensure_ascii=False))

    def config(self, phase):
        return {"output_modalities": ["text" if phase == "route" else "audio"],
                "instructions": (ROUTING + "\nFresh state snapshot (context only; sounds are unconfirmed): "
                                 + json.dumps(self.route_snapshot())
                                 if phase == "route" else ANSWER),
                "tools": TOOLS if phase == "route" else [],
                "tool_choice": "required" if phase == "route" else "none"}

    def route_snapshot(self):
        return {"checklist": [{"sci": r["sci"], "en": r["en"], "count": r["count"]} for r in self.items],
                "pending": [r["en"] for r in self.suggestions.values()],
                "last_heard": list(dict.fromkeys(d["en"] for _, d in reversed(self.history)))[:3],
                "focus": ({"id": obs.id, "top3": obs.fused[:3], "uncertain": obs.uncertain,
                           "hint": obs.hint} if (obs := self.observations.get(self.focus)) else None)}

    async def prune_history(self):
        async with self.history_lock:
            # Function calls and outputs must survive until the whole turn (including
            # its answer) completes, even if a reconnect toggles self.active.
            if self.active or self.turn_in_progress or self.response_waiter is not None:
                return
            deleted = 0
            while (len(self.conversation_ids) > 8 and not self.active
                   and not self.turn_in_progress and self.response_waiter is None):
                item_id = self.conversation_ids[0]
                try:
                    await self.rt_send({"type": "conversation.item.delete", "item_id": item_id})
                except (ConnectionError, websockets.ConnectionClosed):
                    return  # reconnect clears IDs; do not kill the turn worker
                self.conversation_ids.popleft()
                self.item_owners.pop(item_id, None)
                deleted += 1
            if deleted:
                self.log("history_pruned", deleted=deleted, retained=len(self.conversation_ids))

    def session(self):
        return {"model": MODEL, **self.config("route"),
                "audio": {"input": {"format": {"type": "audio/pcm", "rate": 24000},
                                    "turn_detection": {"type": "server_vad", "threshold": 0.5,
                                                       "prefix_padding_ms": 300, "silence_duration_ms": 600,
                                                       "create_response": False, "interrupt_response": False},
                                    "transcription": {"model": "higgs-stt-3.1"}},
                          "output": {"format": {"type": "audio/pcm", "rate": 24000}}}}

    async def update_session(self, config):
        # Reader resolves the echo future; no waiting inside the reader.
        async with self.update_lock:
            self.update_waiter = asyncio.get_running_loop().create_future()
            try:
                await self.rt_send({"type": "session.update", "session": config})
                echo = await asyncio.wait_for(self.update_waiter, 12)
                for key in ("tool_choice", "output_modalities", "instructions"):
                    if key in config and echo.get(key) != config[key]:
                        raise RuntimeError("Session echo mismatch: " + key)
                self.log("session_updated", phase=self.phase)
            finally:
                self.update_waiter = None

    def own_item(self, item_id, owner=None):
        owner = self.turn_items if owner is None else owner
        if item_id and owner is not None:
            owner.add(item_id)
            self.item_owners[item_id] = owner

    async def delete_items(self, items):
        async with self.history_lock:
            for item_id in list(items):
                await self.rt_send({"type": "conversation.item.delete", "item_id": item_id})
                if item_id in self.conversation_ids:
                    self.conversation_ids.remove(item_id)
                self.item_owners.pop(item_id, None)
                self.deleted_item_ids.append(item_id)

    async def generate(self, phase):
        self.phase = phase
        await self.update_session(self.config(phase))
        self.audio_allowed = phase == "answer" and self.may_speak()
        self.first_audio = False
        for attempt in range(2):
            self.response_waiter = asyncio.get_running_loop().create_future()
            self.awaited_response_id = None
            try:
                await self.rt_send({"type": "response.create"})
                try:
                    response = await asyncio.wait_for(asyncio.shield(self.response_waiter), 8 if phase == "route" else 25)
                except asyncio.TimeoutError:
                    self.log("response_timeout", phase=phase)
                    if self.awaited_response_id:
                        self.cancelled_ids.append(self.awaited_response_id)
                    await self.rt_send({"type": "response.cancel"})
                    if phase != "route" or attempt or self.tools_executed:
                        raise
                    continue
                for item in response.get("output", []):
                    self.own_item(item.get("id"))
                if response.get("status") == "failed":
                    if phase == "route" and not attempt and not self.tools_executed:
                        self.log("route_retry", reason="failed")
                        await asyncio.sleep(.3)
                        continue
                    raise RuntimeError("Upstream generation failed")
                return response
            finally:
                self.response_waiter = None

    async def inject(self, text):
        item_id = "item_" + uuid.uuid4().hex[:24]
        self.own_item(item_id)
        await self.rt_send({"type": "conversation.item.create", "item": {"id": item_id,
                            "type": "message", "role": "user",
                            "content": [{"type": "input_text", "text": text}]}})
        return item_id

    def suggestions_payload(self):
        return {"type": "suggestions", "items": [
            {k: r[k] for k in ("sci", "en", "photo", "conf", "t")}
            for r in sorted(self.suggestions.values(), key=lambda r: -r["conf"])]}

    async def broadcast_suggestions(self):
        await self.phone_json(self.suggestions_payload())

    async def suggestion_tick(self, now=None):
        now = time.monotonic() if now is None else now
        expired = [sci for sci, r in self.suggestions.items()
                   if (r["prompt_at"] is not None and now - r["prompt_at"] >= SUGGEST_TTL)
                   or (r["prompt_at"] is None and now - r["last_heard"] > SUGGEST_STALE)
                   or any(i["sci"] == sci for i in self.items)]
        for sci in expired:
            del self.suggestions[sci]
        if expired:
            await self.broadcast_suggestions()
        fresh = [r for r in self.suggestions.values() if r["prompt_at"] is None]
        if (fresh and now - min(r["buffered_at"] for r in fresh) >= SUGGEST_DEBOUNCE
                and now - self.last_proactive >= SUGGEST_INTERVAL
                and not any(p[0] == "notify" for p in self.pending)):
            self.pending.append(("notify", None, None))
        if self.pending:
            self.work_ready.set()

    async def suggestion_loop(self):
        while not self.closed:
            await self.suggestion_tick()
            await asyncio.sleep(.2)

    def suggestion_batch(self):
        now = time.monotonic()
        return sorted((r for r in self.suggestions.values() if r["prompt_at"] is None
                       and now - r["last_heard"] <= SUGGEST_STALE), key=lambda r: -r["conf"])

    async def flush_pending(self):
        self.work_ready.set()

    async def turn_worker(self):
        while not self.closed:
            await self.work_ready.wait()
            self.work_ready.clear()
            if not self.connected or self.active or self.user_speaking or not self.pending:
                continue
            user = next((p for p in self.pending if p[0] in ("text", "audio")), None)
            if user is not None:
                self.pending.remove(user)
                self.pending.appendleft(user)
            kind, value, stopped = self.pending[0]
            if kind == "text" and stopped is not None:
                # Merge consecutive queued utterances into one turn (backlog after a slow turn).
                parts = [value]
                merged_ids = set(getattr(value, "item_ids", ()))
                while len(self.pending) > 1 and self.pending[1][0] == "text" and self.pending[1][2] is not None:
                    _, more, stopped = self.pending[1]
                    del self.pending[1]
                    parts.append(more)
                    merged_ids.update(getattr(more, "item_ids", ()))
                if len(parts) > 1:
                    value = TurnText(" ".join(parts), merged_ids)
                    self.pending[0] = (kind, value, stopped)
                    self.log("turns_merged", n=len(parts))
            self.turn_stale = stopped is not None and time.monotonic() - stopped > 12
            if kind == "notify" and (self.awaiting_transcript or self.quiet
                                     or time.monotonic() < self.play_until
                                     or time.monotonic() - self.last_proactive < SUGGEST_INTERVAL):
                continue
            self.pending.popleft()
            if kind == "notify":
                batch = self.suggestion_batch()
                if not batch:
                    self.work_ready.set()
                    continue
            self.active = True
            self.turn_in_progress = True
            self.turn_stopped_at = stopped
            self.turn_items = TurnItems(getattr(value, "item_ids", ()))
            for item_id in self.turn_items:
                self.item_owners[item_id] = self.turn_items
            self.tools_executed = False
            try:
                if kind == "vision":
                    await self.answer_vision(value)
                elif kind == "notify":
                    self.proactive_turn = True
                    self.last_proactive = time.monotonic()
                    for r in batch:
                        r["prompt_at"] = self.last_proactive
                    spoken = ", ".join(f"{r['en']} ({r['conf']:.2f})" for r in batch[:3])
                    extra = f" Say 'and {len(batch) - 3} more on screen'." if len(batch) > 3 else ""
                    await self.inject("[NOTIFY] BirdNET heard: " + spoken
                                      + ". Ask briefly in one short sentence, first person, e.g. 'I'm hearing a Northern Cardinal and an American Robin, add them?'. Don't mention BirdNET. List at most 3."
                                      + extra + " Do not speak confidence scores. Do not log anything.")
                    self.log("suggestion_prompt", species=[r["sci"] for r in batch],
                             buffered_ms=round((self.last_proactive - min(r["buffered_at"] for r in batch)) * 1000))
                    await self.generate("answer")
                else:
                    if kind == "text":
                        await self.inject(value)
                    if self.filler_only(value):
                        self.log("route_skipped_filler")
                        await self.delete_items(self.turn_items)
                        continue
                    response = await self.generate("route")
                    await self.finish_route(response)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log("turn_failed", error_type=type(exc).__name__)
                self.turn_items.failed = True
                try:
                    await self.delete_items(self.turn_items)
                except (ConnectionError, websockets.ConnectionClosed):
                    # No next route on unreconciled history: reconnect starts clean.
                    self.connected = False
                    if self.rt is not None:
                        await self.rt.close()
                await self.error("Turn failed; please try again.")
            finally:
                self.turn_items = None
                self.proactive_turn = False
                self.active = False
                self.turn_in_progress = False
                self.transcripts.clear()
                if self.connected:
                    await self.prune_history()
                await self.state()
                if self.pending:
                    self.work_ready.set()

    @staticmethod
    def audit_args(arguments):
        def clean(value):
            if isinstance(value, dict):
                return {k: ("[redacted]" if re.search(r"key|token|secret|password|authorization", k, re.I)
                            else clean(v)) for k, v in value.items()}
            if isinstance(value, list):
                return [clean(v) for v in value]
            if isinstance(value, str):
                return re.sub(r"(?i)bearer\s+\S+|\bsk-[\w-]+", "[redacted]", value)
            return value
        try:
            return json.dumps(clean(json.loads(arguments or "{}")), ensure_ascii=False, separators=(",", ":"))[:200]
        except (ValueError, TypeError):
            return "[invalid JSON]"

    async def finish_route(self, response):
        if response.get("status") != "completed":
            return
        calls = [i for i in response.get("output", []) if i.get("type") == "function_call"]
        self.log("route_done", tools=[c["name"] for c in calls],
                 arguments=[self.audit_args(c.get("arguments")) for c in calls])
        if len(calls) == 2 and {c["name"] for c in calls} == {"remove_observation", "dismiss_suggestions"}:
            remove = next(c for c in calls if c["name"] == "remove_observation")
            try:
                clear_all = json.loads(remove.get("arguments") or "{}").get("all") is True
            except (ValueError, TypeError, AttributeError):
                clear_all = False
            if clear_all:
                # The sole allowed compound route: dismiss before clearing outing.
                for call in sorted(calls, key=lambda c: c["name"] != "dismiss_suggestions"):
                    await self.finish_route({"status": "completed", "output": [call]})
                return
        if len(calls) != 1:
            await self.error("Routing did not select one action; please try again.")
            return
        call = calls[0]
        name = call["name"]
        try:
            args = json.loads(call.get("arguments") or "{}")
            self.tools_executed = True
            result = {"ok": True} if name == "ignore" else (
                {"ready_to_answer": True, "reason": args.get("reason")} if name == "speak"
                else await self.tool(name, args))
        except (ValueError, TypeError, KeyError) as exc:
            result = {"ok": False, "error": str(exc)[:180]}
        except Exception as exc:
            self.log("tool_failed", name=name, error_type=type(exc).__name__)
            result = {"ok": False, "error": "External service unavailable; try again"}
        if name in WRITE_TOOLS:
            await self.broadcast_checklist()
            if result.get("ok"):
                await self.phone_json({"type": "earcon", "kind": "logged"})
                self.latency("earcon")
            else:
                await self.error(result.get("error", "Checklist change failed"))
        await self.rt_send({"type": "conversation.item.create", "item": {
            "type": "function_call_output", "call_id": call["call_id"],
            "output": json.dumps(result, ensure_ascii=False)}})
        self.log("tool", name=name, arguments=self.audit_args(call.get("arguments")), ok=result.get("ok", True))
        if result.get("suppress_answer"):
            return
        if name == "ask_expert" and result.get("context") and not self.expert_result_current(result):
            return
        if name in ("speak", "get_checklist", "recent_sounds", "nearby_species", "get_observation", "ask_expert", "describe") and self.may_speak():
            if (getattr(self, "turn_stale", False) or (self.turn_stopped_at is not None
                    and time.monotonic() - self.turn_stopped_at > 12)):
                self.log("stale_answer_skipped")  # answering a question asked >12 s ago is confusing
                return
            await self.generate("answer")

    def latency(self, target):
        if self.turn_stopped_at is not None:
            self.log("latency", target=target,
                     speech_stopped_ms=round((time.monotonic() - self.turn_stopped_at) * 1000, 1))

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
                    self.awaited_response_id = None
                    self.user_speaking = self.awaiting_transcript = False
                    self.conversation_ids.clear()
                    self.deleted_item_ids.clear()
                    self.item_owners.clear()
                    self.response_owners.clear()
                    self.transcripts.clear()
                    # Higgs waits for the first update before emitting session.created.
                    await self.rt_send({"type": "session.update", "session": self.session()})
                    while True:
                        event = json.loads(await asyncio.wait_for(ws.recv(), 20))
                        if event.get("type") in ("session.created", "session.updated"):
                            break
                        if event.get("type") == "error":
                            raise RuntimeError("Realtime setup rejected")
                    self.connected = True
                    delay = 1
                    self.log("realtime_connected", model=MODEL, acknowledgement=event.get("type"))
                    self.log("history_strategy", strategy="conversation.item.delete", keep=8)
                    summary = "; ".join(f"{r['count']} {r['en']} ({r.get('zh', '')})" for r in self.items) or "empty"
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
                for waiter in (self.update_waiter, self.response_waiter):
                    if waiter is not None and not waiter.done():
                        waiter.set_exception(ConnectionError("Realtime disconnected"))
                self.rt = None
                self.connected = False
                self.user_speaking = self.awaiting_transcript = False
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
        if ty in ("conversation.item.created", "conversation.item.added"):
            item = event.get("item", {})
            item_id = item.get("id")
            if item_id in self.deleted_item_ids:
                return  # delayed create acknowledgement after failed-turn deletion
            # User audio may arrive while an older turn is routing. Its transcript
            # assigns ownership later; never sweep it into the active turn.
            if item.get("role") != "user" and item_id not in self.item_owners:
                self.own_item(item_id)
            owner = self.item_owners.get(item_id)
            if owner is not None and getattr(owner, "failed", False):
                await self.delete_items([item_id])
                return
            if item_id and item_id not in self.conversation_ids:
                self.conversation_ids.append(item_id)
            if not self.active:
                await self.prune_history()
            return
        if ty == "response.output_item.added":
            item_id = event.get("item", {}).get("id")
            owner = self.response_owners.get(rid)
            self.own_item(item_id, owner)
            if owner is not None and getattr(owner, "failed", False) and item_id:
                await self.delete_items([item_id])
            return
        if ty == "session.updated":
            if self.update_waiter is not None and not self.update_waiter.done():
                self.update_waiter.set_result(event.get("session", {}))
            return
        if ty in output_types and (self.phase != "answer" or not self.audio_allowed):
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
                for waiter in (self.update_waiter, self.response_waiter):
                    if waiter is not None and not waiter.done():
                        waiter.set_exception(RuntimeError("Realtime request rejected"))
                await self.error("Realtime request failed (" + str(code)[:100] + ").")
        elif ty == "input_audio_buffer.speech_started":
            if self.echo_blocked():
                self.log("vad_ignored", reason="assistant_playback")
                return
            self.input_revision += 1
            self.user_speaking = True
            self.awaiting_transcript = True
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
            self.speech_stopped_at = time.monotonic()
        elif ty == "conversation.item.input_audio_transcription.completed":
            self.awaiting_transcript = False
            text = event.get("transcript", "")
            # With create_response=false the server never commits the buffer, so STT
            # re-transcribes everything since session start. Clear it per turn (tested 10/23).
            await self.rt_send({"type": "input_audio_buffer.clear"})
            self.user_speaking = False
            if self.transcript_is_echo(text):
                self.log("echo_dropped", text=text)
                await self.flush_pending()
                return
            await self.phone_json({"type": "transcript", "role": "user", "text": text, "final": True})
            self.log("user_transcript", text=text)
            if text.strip():
                self.input_revision += 1
                self.pending.append(("text", TurnText(text, [event["item_id"]] if event.get("item_id") else []), self.speech_stopped_at))  # transcript as text; audio buffer is cleared
                await self.flush_pending()
        elif ty == "conversation.item.input_audio_transcription.failed":
            self.awaiting_transcript = False
            await self.error("Input transcription failed.")
            await self.flush_pending()
        elif ty == "response.created":
            self.active = True
            self.response_id = event["response"]["id"]
            if self.turn_items is not None:
                self.response_owners[self.response_id] = self.turn_items
                while len(self.response_owners) > 100:
                    del self.response_owners[next(iter(self.response_owners))]
            if self.response_id not in self.cancelled_ids and self.response_waiter is not None:
                self.awaited_response_id = self.response_id
        elif ty in ("response.output_audio.delta", "response.audio.delta"):
            if rid in self.cancelled_ids or not self.audio_allowed:
                return
            pcm = base64.b64decode(event["delta"])
            was_speaking = time.monotonic() < self.play_until
            # Close the gate BEFORE sending audio: mic ingestion runs concurrently.
            self.play_until = max(time.monotonic(), self.play_until) + len(pcm) / 48000
            self.echo_estimate = self.play_until
            if not self.first_audio:
                for client in self.playback_clients():
                    client.playback_end = 0.0
            await self.phone_audio(pcm)
            self.total_audio_out += len(pcm)
            if not self.first_audio:
                self.first_audio = True
                if self.proactive_turn:
                    self.last_proactive = time.monotonic()
                self.latency("first_audio")
            if not was_speaking:
                await self.state()
            if self.playback_task is None or self.playback_task.done():
                self.playback_task = asyncio.create_task(self.playback_finished())
        elif ty in ("response.output_audio_transcript.delta", "response.audio_transcript.delta", "response.output_text.delta"):
            if rid in self.cancelled_ids or not self.audio_allowed:
                return
            key = (rid, event.get("item_id", ""), "text" if "output_text" in ty else "audio")
            self.transcripts[key] = self.transcripts.get(key, "") + event.get("delta", "")
            if "output_text" not in ty:
                self.remember_spoken(key, self.transcripts[key])
            await self.phone_json({"type": "transcript", "role": "assistant", "text": self.transcripts[key], "final": False})
        elif ty in ("response.output_audio_transcript.done", "response.audio_transcript.done", "response.output_text.done"):
            if rid not in self.cancelled_ids and self.audio_allowed:
                if "output_text" not in ty:
                    self.remember_spoken((rid, event.get("item_id", ""), "audio"), event.get("transcript", ""))
                await self.phone_json({"type": "transcript", "role": "assistant",
                                       "text": event.get("transcript", event.get("text", "")), "final": True})
        elif ty == "response.done":
            response = event["response"]
            self.log("response_done", status=response.get("status"), phase=self.phase)
            if (self.response_waiter is not None and not self.response_waiter.done()
                    and response.get("id") == self.awaited_response_id
                    and response.get("id") not in self.cancelled_ids):
                self.response_waiter.set_result(response)

    async def find_item(self, name):
        needle = str(name).strip().casefold()
        for row in self.items:
            if any(str(row.get(k, "")).casefold() == needle for k in ("id", "sci", "en", "zh")):
                return row["id"]
        taxon = await asyncio.to_thread(resolve, str(name), None)
        if taxon:
            return next((r["id"] for r in self.items if r["sci"] == taxon["sci"]), None)
        return None

    async def log_taxon(self, taxon, count, notes, source="voice"):
        """Write confirmed evidence; checklist rows remain derived observations."""
        group = taxon.get("group") or "Aves"
        active = self.observations.get(self.focus)
        # Confirming background sound must not discard an open camera focus.
        if source == "birdnet" and active and active.status == "open":
            obs = Observation("ob" + uuid.uuid4().hex[:10], lat=self.lat, lng=self.lng, group=group)
            self.observations[obs.id] = obs
        else:
            obs = await self.new_focus(group)
        candidate = {k: taxon.get(k, "") for k in ("sci", "en", "zh", "photo")}
        candidate["p"] = float(taxon.get("conf", 1)) if source == "birdnet" else 1.0
        obs.detections.append(Detection("d" + uuid.uuid4().hex[:12],
            "sound" if source == "birdnet" else "description", now_iso(), [candidate],
            text=None if source == "birdnet" else candidate["en"]))
        # Explicit confirmation is never removed by the local-range filter.
        obs.fused, obs.uncertain = fuse(obs.detections)
        obs.count, obs.notes, obs.status = count, notes, "confirmed"
        await self.broadcast_observation(obs)
        return next(dict(r) for r in self.items if r["sci"] == taxon["sci"])

    async def tool(self, name, args):
        if name == "look":
            return await self.request_look(args.get("hint", ""), args.get("group"))
        if name == "new_observation":
            group = args.get("group", "Aves")
            if group not in GROUPS:
                raise ValueError("invalid iconic group")
            return {"ok": True, "observation": (await self.new_focus(group)).payload()}
        if name in ("get_observation", "confirm_observation", "discard_observation", "ask_expert", "describe"):
            obs = await self.current_focus(create=name == "describe")
            if not obs:
                return {"ok": False, "error": "No focus observation; look first"}
            if name == "get_observation":
                return {"observation": obs.payload()}
            if name == "ask_expert":
                return await self.ask_expert(args["question"], obs)
            if name == "discard_observation":
                obs.status = "discarded"
                obs.awaiting_answer = False
                self.focus = None
                await self.broadcast_observation(obs)
                await self.broadcast_checklist()
                return {"ok": True}
            if name == "describe":
                candidates = []
                # Never autocomplete arbitrary descriptive prose into a species.
                if args.get("name"):
                    taxon = await asyncio.to_thread(resolve, str(args["name"]), obs.group)
                    if taxon:
                        candidates = [{**taxon, "p": 1.0}]
                obs.detections.append(Detection("d" + uuid.uuid4().hex[:12], "description", now_iso(), candidates, text=str(args["text"])))
                await self.refusion(obs)
                return {"ok": True, "observation": obs.payload()}
            if not obs.fused or obs.status == "discarded":
                return {"ok": False, "error": "No identified candidate to confirm; look again"}
            obs.count = valid_count(args.get("count", obs.count or 1))
            obs.notes = str(args.get("notes", obs.notes))
            obs.status = "confirmed"
            obs.awaiting_answer = False
            await self.broadcast_observation(obs)
            return {"ok": True, "observation": obs.payload()}
        if name == "get_checklist":
            return {"items": [dict(r) for r in self.items], "species_count": len(self.items)}
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
        if name in ("accept_suggestions", "dismiss_suggestions"):
            await self.suggestion_tick()
            species = args.get("species") or []
            if not isinstance(species, list) or any(not isinstance(n, str) for n in species):
                raise ValueError("species must be an array of names")
            selected = list(self.suggestions.values()) if not species else []
            for n in species:
                needle = n.strip().casefold()
                matches = [r for r in self.suggestions.values()
                           if needle in (r["sci"].casefold(), r["en"].casefold())]
                if not matches:
                    # Allow common short forms such as 'cardinal', only if unambiguous.
                    matches = [r for r in self.suggestions.values() if needle and needle in r["en"].casefold()]
                if len(matches) != 1:
                    raise ValueError("Unknown or ambiguous pending suggestion: " + n)
                if matches[0] not in selected:
                    selected.append(matches[0])
            if not selected:
                return {"ok": False, "error": "No pending suggestions"}
            counts = args.get("counts") or {}
            if not isinstance(counts, dict):
                raise ValueError("counts must be an object")
            prepared = []
            for r in selected:
                count = valid_count(next((v for k, v in counts.items() if k.casefold() in
                                          (r["sci"].casefold(), r["en"].casefold())), 1))
                prepared.append((r, count, ""))
            async with self.item_lock:
                rows = [await self.log_taxon(r, count, notes, "birdnet") for r, count, notes in prepared] if name == "accept_suggestions" else []
                for r in selected:
                    self.suggestions.pop(r["sci"], None)
            await self.broadcast_suggestions()
            return {"ok": True, "items": rows}
        if name in ("add_observation", "add_observations"):
            entries = [args] if name == "add_observation" else args.get("items")
            if not isinstance(entries, list) or not entries or len(entries) > 100:
                raise ValueError("items must contain 1–100 observations")
            prepared = []
            for entry in entries:
                count = valid_count(entry.get("count", 1))
                group = entry.get("group", "Aves")
                if group not in GROUPS:
                    raise ValueError("invalid iconic group")
                taxon = await asyncio.to_thread(resolve, str(entry["name"]), group)
                if not taxon:
                    return {"ok": False, "error": "No matching taxon in that iconic group"}
                prepared.append((taxon, count, str(entry.get("notes", ""))))
            async with self.item_lock:
                rows = [await self.log_taxon(taxon, count, notes) for taxon, count, notes in prepared]
                for taxon, _, _ in prepared:
                    self.suggestions.pop(taxon["sci"], None)
            if any(taxon["sci"] in self.notified for taxon, _, _ in prepared):
                await self.broadcast_suggestions()
            return {"ok": True, "items": rows, "item": rows[0],
                    "observation": self.observations[self.focus].payload()}
        if name == "remove_observation" and args.get("all") is True:
            await self.clear_outing()
            return {"ok": True, "items": []}
        if name in ("remove_observation", "set_count"):
            count = valid_count(args.get("count")) if name == "set_count" else None
            item_id = await self.find_item(args["name"])
            if not item_id:
                return {"ok": False, "error": "Species is not in the checklist"}
            row = await self.change_checklist(item_id, count)
            return {"ok": True, "item": row}
        raise ValueError("Unknown tool")

    def analyze(self, samples):
        # All analyzer access stays on this connection's single worker thread.
        from birdnetlib import RecordingBuffer
        from birdnetlib.analyzer import Analyzer
        if self.analyzer is None:
            self.analyzer = Analyzer()
        rec = RecordingBuffer(self.analyzer, samples, SR, min_conf=0.25)
        rec.analyze()
        return eligible_detections(rec.detections, self.allowed_species())

    def allowed_species(self):
        region = (round(self.lat, 1), round(self.lng, 1))
        key = (*region, date.today().isoformat())
        now = time.monotonic()
        if getattr(self, "_allowed_key", None) == key and now < self._allowed_retry:
            return self._allowed
        cache = ALLOWLIST_DIR / f"allowlist-{region[0]:.1f}_{region[1]:.1f}.json"
        previous = set()
        try:
            previous = {n for n in json.loads(cache.read_text())["species"] if scientific_species(n)}
        except (OSError, ValueError, KeyError, TypeError):
            pass
        if getattr(self, "_allowed_region", None) == region:
            previous |= getattr(self, "_allowed", set())
        try:
            from birdnetlib.species import SpeciesList
            allowed = {x["scientific_name"] for x in SpeciesList().return_list(
                lon=self.lng, lat=self.lat, date=datetime.now(), threshold=0.001)}
            params = {"iconic_taxa": "Aves", "verifiable": "true", "rank": "species", "per_page": 500}
            if 41.7 <= self.lat <= 56.9 and -95.2 <= self.lng <= -74.3:
                params["place_id"] = 6883
            else:
                params.update(lat=self.lat, lng=self.lng, radius=100)
            page = 1
            priors = {}
            while True:
                result = inat("observations/species_counts", **params, page=page)
                rows = result["results"]
                priors.update({r["taxon"]["name"]: r.get("count", 0) for r in rows})
                allowed.update(r["taxon"]["name"] for r in rows
                               if r["taxon"].get("rank") == "species"
                               and r["taxon"].get("iconic_taxon_name") == "Aves")
                if len(rows) < 500:
                    break
                page += 1
            allowed = {n for n in allowed if scientific_species(n)}
            if not allowed:
                raise ValueError("Empty allowlist")
        except Exception as exc:
            allowed = previous
            self.log("allowlist_unavailable", error_type=type(exc).__name__,
                     fallback=len(allowed), suppress=not bool(allowed))
            retry = now + 60
        else:
            self._prior_counts = priors
            retry = float("inf")
            try:
                cache.parent.mkdir(parents=True, exist_ok=True)
                temp = cache.with_suffix(".tmp")
                temp.write_text(json.dumps({"date": key[2], "species": sorted(allowed)}))
                temp.replace(cache)
            except OSError as exc:
                self.log("allowlist_cache_failed", error_type=type(exc).__name__)
        self._allowed_key, self._allowed_region = key, region
        self._allowed, self._allowed_retry = allowed, retry
        self.log("allowed_species", n=len(allowed))
        return allowed

    async def bird_window(self, samples, end):
        try:
            epoch = self.epoch
            start = time.monotonic()
            detections = await asyncio.get_running_loop().run_in_executor(self.executor, self.analyze, samples)
            if epoch != self.epoch:
                return
            self.log("bird_window", stream_s=round(end / SR, 2), count=len(detections),
                     compute_ms=round((time.monotonic() - start) * 1000))
            sound_candidates = []
            for d in detections:
                sci = d.get("scientific_name") or d["common_name"]
                if sci not in self.names:
                    try:
                        taxon = await asyncio.to_thread(resolve, sci, "Aves")
                    except Exception:
                        taxon = None
                    if epoch != self.epoch:
                        return
                    self.names[sci] = taxon or {"sci": sci, "en": d["common_name"], "zh": d["common_name"]}
                names = {k: self.names[sci].get(k, "") for k in ("sci", "en", "zh", "photo")}
                confidence = round(float(d["confidence"]), 4)
                sound_candidates.append({**names, "p": confidence})
                hits = self.windows[sci]
                while hits and end - hits[0] > 10 * SR:
                    hits.popleft()
                if confidence >= .5 and (not hits or hits[-1] != end):
                    hits.append(end)
                qualified = confidence >= .7 or (confidence >= .5 and len(hits) >= 2)
                first = qualified and sci not in self.seen
                heard = {"type": "heard", **names, "conf": confidence, "t": now_iso(), "new": first,
                         "lat": self.lat, "lng": self.lng, "unusual": False}
                self.history.append((time.monotonic(), {k: v for k, v in heard.items() if k != "type"}))
                if qualified:
                    self.seen.add(sci)
                    await self.phone_json(heard)
                self.log("bird_detection", **names, conf=confidence, stream_s=round(end / SR, 2))
                if first:
                    await self.phone_json({"type": "earcon", "kind": "heard"})
                now = time.monotonic()
                if sci in self.suggestions:
                    r = self.suggestions[sci]
                    r.update(conf=max(r["conf"], confidence), last_heard=now, t=heard["t"])
                    await self.broadcast_suggestions()
                elif qualified and sci not in self.notified and not any(r["sci"] == sci for r in self.items):
                    self.notified.add(sci)
                    best = max((h["conf"] for t, h in self.history if t >= now - 10 and h["sci"] == sci), default=confidence)
                    self.suggestions[sci] = {**names, "conf": best, "t": heard["t"],
                        "first_heard": now - (end - hits[0]) / SR, "last_heard": now,
                        "buffered_at": now, "prompt_at": None}
                    await self.broadcast_suggestions()
                    self.log("suggestion_buffered", sci=sci, windows=len(hits))
            if sound_candidates:
                stamp = time.monotonic() - max(0, (self.position - end) / SR) - 1.5
                detection = Detection("d" + uuid.uuid4().hex[:12], "sound", now_iso(),
                                      sorted(sound_candidates, key=lambda c: -c["p"])[:5])
                self.sound_windows.append((stamp, detection))
                await self.expire_focus()
                obs = self.observations.get(self.focus)
                if obs and obs.status == "open" and abs(stamp - obs.touched) <= 10:
                    obs.detections.append(detection)
                    await self.refusion(obs, touch=False)
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
        contaminated = self.speaker_mode and self.echo_blocked()
        self.echo_ring[pos:pos + first] = contaminated
        self.ring[pos:pos + first] = frame[:first]
        if first < n:
            self.ring[:n - first] = frame[first:]
            self.echo_ring[:n - first] = contaminated
        self.position += n
        self.total_audio_in += len(raw)
        # Stateful 63-tap anti-alias filter, ~0.65 ms delay; phase survives odd-length packets.
        filtered, self.filter_state = lfilter(self.filter_taps, [1.0], frame, zi=self.filter_state)
        pcm = (np.clip(filtered[self.decimation_phase::2], -1, 1) * 32767).astype("<i2").tobytes()
        self.decimation_phase = (self.decimation_phase - n) % 2
        if self.connected and not self.echo_blocked():
            if self.audio_queue.full():
                self.audio_queue.get_nowait()  # bounded latency rather than stale audio after a network stall
                self.log("audio_queue_drop")
            self.audio_queue.put_nowait(pcm)
        while self.position >= self.next_window:
            indices = np.arange(self.next_window - 3 * SR, self.next_window) % len(self.ring)
            if self.echo_ring[indices].any():
                self.log("bird_window_skipped", reason="assistant_playback", stream_s=round(self.next_window / SR, 2))
            elif self.bird_task is None or self.bird_task.done():
                end = self.next_window
                samples = self.ring[np.arange(end - 3 * SR, end) % len(self.ring)].copy()
                self.bird_task = asyncio.create_task(self.bird_window(samples, end))
            else:
                self.log("bird_window_skipped", reason="worker busy")
            self.next_window += int(1.5 * SR)

    def echo_blocked(self):
        return self.echo_guard and time.monotonic() < self.echo_deadline()

    async def audio_sender(self):
        while True:
            pcm = await self.audio_queue.get()
            try:
                if self.connected and not self.echo_blocked():
                    await self.rt_send({"type": "input_audio_buffer.append", "audio": base64.b64encode(pcm).decode()})
            except Exception as exc:
                self.log("audio_send_failed", error_type=type(exc).__name__)
            finally:
                self.audio_queue.task_done()

    async def handle_phone(self, event, client=None):
        client = client or self.mic or next((c for c in self.clients if c.role == "mic"), None)
        ty = event.get("type")
        if ty == "audio_mode" or (ty == "hello" and "earphones" in event):
            if not isinstance(event.get("earphones"), bool):
                raise ValueError("earphones must be boolean")
            if client is None or client.role != "mic":
                raise ValueError("Only the microphone can set audio_mode")
            client.earphones = event["earphones"]
            self.log("audio_mode", earphones=client.earphones)
            if ty == "audio_mode":
                await self.state()
                return
        if ty == "playback":
            if not isinstance(event.get("active"), bool):
                raise ValueError("playback.active must be boolean")
            if client is None or (client.role == "display" and not client.audio):
                raise ValueError("Client has no audio output")
            client.playback_active = event["active"]
            if not client.playback_active:
                client.playback_end = time.monotonic()
            self.log("playback", active=client.playback_active, role=client.role)
            await self.state()
            return
        if ty == "reset":
            await self.reset()
            return
        if ty in ("hello", "location"):
            if ty == "location" and (event.get("lat") is None or event.get("lng") is None):
                raise ValueError("location requires lat and lng")
            if event.get("lat") is not None and event.get("lng") is not None:
                lat, lng = float(event["lat"]), float(event["lng"])
                if not (-90 <= lat <= 90 and -180 <= lng <= 180):
                    raise ValueError("Invalid GPS coordinates")
                self.lat, self.lng = lat, lng
            if ty == "location":
                await self.state()
                return
            self.lang = str(event.get("lang", "en"))[:32]
            await self.state()
            await self.phone_json({"type": "checklist", "items": [dict(r) for r in self.items]})
            await self.broadcast_suggestions()
        elif ty == "suggestion_action":
            action = event.get("action")
            if action not in ("accept", "dismiss"):
                raise ValueError("Invalid suggestion action")
            result = await self.tool(action + "_suggestions", {"species": [event["sci"]]})
            if not result.get("ok"):
                raise ValueError(result["error"])
            await self.broadcast_checklist()
            await self.phone_json({"type": "earcon", "kind": "logged"})
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
            await self.state()
            await self.flush_pending()
        elif ty == "tap":
            self.input_revision += 1
            self.tap_at = time.monotonic()
            await self.cancel_speech()
            if self.connected:
                await self.inject("[TAP] The next user utterance is addressed to you.")
        elif ty == "text":
            text = event.get("text", "")
            if not isinstance(text, str) or not text.strip() or len(text) > 8000:
                raise ValueError("text must contain 1–8000 characters")
            self.input_revision += 1
            self.pending.append(("text", text, None))
            await self.flush_pending()
        elif ty == "look":
            self.input_revision += 1
            await self.request_look(event.get("hint", ""), event.get("group"))
        elif ty == "observation_action":
            obs = self.observations.get(event.get("id"))
            if not obs or obs.status == "discarded":
                raise ValueError("Unknown or discarded observation")
            action = event.get("action")
            if action not in ("focus", "confirm", "discard"):
                raise ValueError("Unknown observation action")
            old = self.observations.get(self.focus)
            self.focus = obs.id
            obs.touched = time.monotonic()
            if old and old is not obs:
                if old.status == "open":
                    old.status = "discarded"
                    old.awaiting_answer = False
                await self.broadcast_observation(old)
            if action == "focus":
                await self.broadcast_observation(obs)
            else:
                args = {k: event[k] for k in ("count", "notes") if k in event}
                result = await self.tool(action + "_observation", args)
                if not result.get("ok"):
                    raise ValueError(result.get("error", "Action failed"))
                await self.broadcast_checklist()
                if action == "confirm":
                    await self.phone_json({"type": "earcon", "kind": "logged"})
        elif ty in ("edit", "delete"):
            count = valid_count(event.get("count")) if ty == "edit" else None
            await self.change_checklist(event.get("id"), count)
            await self.phone_json({"type": "earcon", "kind": "logged"})
            if self.connected:
                summary = json.dumps(self.items, ensure_ascii=False)
                await self.rt_send({"type": "conversation.item.create", "item": {"type": "message", "role": "user",
                                    "content": [{"type": "input_text", "text": "Manual checklist update (context only): " + summary[:6000]}]}})
        else:
            raise ValueError("Unknown message type")

    async def clear_outing(self):
        # Checklist is derived: mutating self.items would only clear a temporary list.
        self.epoch += 1
        self.input_revision += 1  # invalidate in-flight expert/frame work
        async with self.item_lock:
            self.observations.clear()
            self.focus = None
            self.frames.clear()
            self.sound_windows.clear()
            self.suggestions.clear()
        for rows in (self.history, self.windows, self.seen, self.notified):
            rows.clear()
        self.pending = deque(p for p in self.pending if p[0] not in ("notify", "vision"))
        self.last_proactive = float("-inf")
        await self.phone_json({"type": "reset"})
        await self.broadcast_checklist()
        await self.broadcast_suggestions()

    async def reset(self):
        self.epoch += 1
        if self.turn_task is not None:
            self.turn_task.cancel()
            await asyncio.gather(self.turn_task, return_exceptions=True)
            self.tasks.remove(self.turn_task)
            self.turn_task = None
        try:
            await self.cancel_speech()
        except (ConnectionError, websockets.ConnectionClosed):
            pass
        self.connected = False
        await self.clear_outing()
        for rows in (self.history, self.windows, self.seen, self.notified, self.pending,
                     self.spoken, self.transcripts, self.conversation_ids):
            rows.clear()
        self.last_proactive = float("-inf")
        self.user_speaking = self.awaiting_transcript = self.active = False
        self.response_id = self.awaited_response_id = None
        self.turn_items = None
        self.deleted_item_ids.clear()
        self.item_owners.clear()
        self.response_owners.clear()
        self.tools_executed = False
        self.play_until = self.echo_estimate = self.tap_at = 0
        self.ring.fill(0)
        self.echo_ring.fill(False)
        self.position, self.next_window = 0, 3 * SR
        self.filter_state.fill(0)
        self.decimation_phase = 0
        while not self.audio_queue.empty():
            self.audio_queue.get_nowait()
            self.audio_queue.task_done()
        for client in self.clients:
            client.playback_active = False
            client.playback_end = 0
        if self.rt is not None:
            await self.rt.close()
        if self.tasks:
            self.turn_task = asyncio.create_task(self.turn_worker())
            self.tasks.append(self.turn_task)
        await self.phone_json({"type": "flush"})
        await self.broadcast_checklist()
        await self.broadcast_suggestions()
        await self.state()
        self.log("reset")

    async def cancel_speech(self):
        if self.response_id:
            self.cancelled_ids.append(self.response_id)
        self.audio_allowed = False
        self.play_until = 0
        if self.connected and (self.active or self.response_id):
            await self.rt_send({"type": "response.cancel"})
        await self.phone_json({"type": "flush"})
        await self.state()

    def start(self):
        self.turn_task = asyncio.create_task(self.turn_worker())
        self.tasks = [asyncio.create_task(self.realtime_loop()),
                      asyncio.create_task(self.audio_sender()), self.turn_task,
                      asyncio.create_task(self.focus_watch()),
                      asyncio.create_task(self.suggestion_loop())]

    async def close(self):
        self.closed = True
        tasks = self.tasks + [t for t in (self.bird_task, self.playback_task) if t is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.executor.shutdown(wait=False, cancel_futures=True)

    async def expire(self):
        await asyncio.sleep(SESSION_TTL)
        if not self.clients:
            SESSIONS.pop(self.id, None)
            await self.close()


class Client:
    def __init__(self, phone, session, role="mic", audio=False):
        self.phone, self.session, self.role, self.audio = phone, session, role, audio
        self.lock = asyncio.Lock()
        self.earphones = False
        self.playback_active = False
        self.playback_end = 0.0

    async def send_json(self, event):
        async with self.lock:
            await self.phone.send_json(event)

    async def send_audio(self, pcm):
        async with self.lock:
            await self.phone.send_bytes(pcm)

    async def run(self):
        s = self.session
        if s.expiry_task:
            s.expiry_task.cancel()
            s.expiry_task = None
        # Attach before awaiting old.close: its finally must not start an expiry
        # timer during the transient microphone replacement.
        s.clients.add(self)
        if self.role == "mic":
            old = s.mic
            s.mic = self
            if old is not None:
                s.clients.discard(old)
                try:
                    await old.phone.close(code=1000, reason="Replaced by newer microphone")
                except (RuntimeError, WebSocketDisconnect):
                    pass
        try:
            await s.state()
            await self.send_json({"type": "checklist", "items": [dict(r) for r in s.items]})
            for obs in list(s.observations.values()):
                await self.send_json({"type": "observation", **obs.payload(), "focus": s.focus == obs.id})
            await self.send_json(s.suggestions_payload())
            while True:
                packet = await self.phone.receive()
                if packet["type"] == "websocket.disconnect":
                    break
                if self.role == "mic" and s.mic is not self:
                    break  # a replaced mic cannot race a final JSON control message
                try:
                    if packet.get("bytes") is not None:
                        if self.role == "mic" and s.mic is self:
                            await s.ingest_audio(packet["bytes"])
                    elif packet.get("text") is not None:
                        event = json.loads(packet["text"])
                        if not isinstance(event, dict):
                            raise ValueError("JSON message must be an object")
                        if self.role == "display" and event.get("type") not in ("hello", "text", "edit", "delete", "tap", "suggestion_action", "playback", "look", "observation_action", "reset"):
                            raise ValueError("Display cannot control microphone or mode")
                        if event.get("type") == "hello" and self.role == "display":
                            await self.send_json(s.state_payload())
                            await self.send_json({"type": "checklist", "items": [dict(r) for r in s.items]})
                        else:
                            await s.handle_phone(event, self)
                except (ValueError, KeyError, TypeError) as exc:
                    await s.error(str(exc)[:180])
                except Exception as exc:
                    s.log("client_message_failed", error_type=type(exc).__name__)
                    await s.error("Request could not be completed")
        except WebSocketDisconnect:
            pass
        finally:
            s.clients.discard(self)
            if s.mic is self:
                s.mic = None
            await s.state()
            if not s.clients:
                s.expiry_task = asyncio.create_task(s.expire())


app = FastAPI(title="Field Companion v0.2")


@app.websocket("/ws")
async def phone_socket(ws: WebSocket):
    role = ws.query_params.get("role", "mic")
    session_id = ws.query_params.get("session", "demo")
    if role not in ("mic", "display") or not session_id or len(session_id) > 128:
        await ws.close(code=1008)
        return
    await ws.accept()
    session = SESSIONS.get(session_id)
    if session is None:
        session = SESSIONS[session_id] = Session(session_id)
        session.start()
    await Client(ws, session, role, ws.query_params.get("audio") == "1").run()


@app.post("/api/frame")
async def post_frame(request: Request, session: str, obs: str, x: float | None = None,
                     y: float | None = None, w: float | None = None, h: float | None = None):
    s = SESSIONS.get(session)
    if not s:
        raise HTTPException(404, "Unknown session; connect WebSocket first")
    await s.expire_focus()
    observation = s.observations.get(obs)
    if not observation or observation.status != "open" or s.focus != obs:
        raise HTTPException(409, "Frame requires the current open focus observation")
    crop = None
    if any(v is not None for v in (x, y, w, h)):
        if any(v is None or not math.isfinite(v) for v in (x, y, w, h)) or not (0 <= x < 1 and 0 <= y < 1 and 0 < w <= 1 and 0 < h <= 1 and x + w <= 1 and y + h <= 1):
            raise HTTPException(422, "Crop must be normalized x,y,w,h within the frame")
        crop = [x, y, w, h]
    jpeg = await request.body()
    if len(jpeg) > 8 * 1024 * 1024:
        raise HTTPException(413, "JPEG must be at most 8 MiB")
    try:
        from PIL import Image
        image = Image.open(io.BytesIO(jpeg))
        if image.format != "JPEG" or max(image.size) > 1920:
            raise ValueError()
        image.thumbnail((512, 512))
        thumb = io.BytesIO()
        image.convert("RGB").save(thumb, format="JPEG", quality=80)
    except Exception:
        raise HTTPException(422, "Expected valid JPEG with long side <=1920 pixels")
    allowed = await asyncio.get_running_loop().run_in_executor(s.executor, s.allowed_species) if observation.group == "Aves" else None
    result = await asyncio.to_thread(classify_frame, jpeg, crop, sorted(allowed) if allowed else None)
    candidates = []
    for candidate in result["candidates"]:
        sci = candidate["sci"]
        if sci not in s.names:
            try:
                taxon = await asyncio.to_thread(resolve, sci, observation.group)
            except Exception:
                taxon = None
            s.names[sci] = taxon or {"sci": sci, "en": sci}
        candidates.append({**s.names[sci], "sci": sci, "p": float(candidate["p"])})
    # Vision is slow: discard late frames if focus changed while inference ran.
    await s.expire_focus()
    if observation.status != "open" or s.focus != obs:
        raise HTTPException(409, "Observation closed or focus changed during classification")
    detection_id = "d" + uuid.uuid4().hex[:12]
    detection = Detection(detection_id, "image", now_iso(), candidates, thumb="/api/frame/" + detection_id + ".jpg")
    s.frames[detection_id] = thumb.getvalue()
    observation.detections.append(detection)
    await s.refusion(observation)
    if observation.awaiting_answer:
        observation.awaiting_answer = False
        s.pending.append(("vision", obs, None))
        await s.flush_pending()
    return {"detection": asdict(detection)}


@app.get("/api/frame/{detection_id}.jpg")
async def get_frame(detection_id: str):
    for s in SESSIONS.values():
        if detection_id in s.frames:
            return Response(s.frames[detection_id], media_type="image/jpeg")
    raise HTTPException(404, "Unknown frame")


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
