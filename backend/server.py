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
from investigations import ACTIVE, InvestigationQueue, QueueFull
from tool_feedback import TOOLS as FEEDBACK_TOOLS, recording as feedback_recording
from route_safety import route_guard
from realtime_provider import RealtimeConfig

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
    analysis_status: str = "idle"
    analysis_stage: str = "idle"
    analysis_request_id: str | None = None
    analysis_created_at: str | None = None
    image_revision: int = 0
    analysis_started: float | None = field(default=None, repr=False)
    analysis_finished: float | None = field(default=None, repr=False)

    def payload(self):
        return {k: v for k, v in asdict(self).items()
                if k not in ("touched", "awaiting_answer", "analysis_started", "analysis_finished")}


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
    uncertain = fused[0]["p"] < .5 or margin < .2
    images = [d for d in detections if d.kind == "image"]
    if images:
        # Local abundance and renormalization may reorder ranks, but cannot turn
        # weak visual evidence into a confident identity. Frames are correlated.
        image = images[-1]
        raw = {c["sci"]: float(c["p"]) for c in image.candidates}
        winner = fused[0]["sci"]
        support = raw.get(winner, 0)
        alternative = max((p for sci, p in raw.items() if sci != winner), default=0)
        uncertain |= support < .5 or support - alternative < .2
    return fused, uncertain


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
        m = re.search(r"CLIENT_BUILD = '([^']+)'", (Path(__file__).resolve().parent.parent / "web" / "app.js").read_text(encoding="utf-8"))
        return m.group(1) if m else "unknown"
    except OSError:
        return "unknown"


SERVER_BUILD = _web_build()

NAME = {"type": "string"}
COUNT = {"type": "integer", "minimum": 1}
TASK_SELECTOR = {"id": NAME, "selection": {"type": "string", "enum": ["first", "latest", "all"]}}
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
    tool_schema("look", "Prepare a photo: target=new identifies a new unknown organism with independent evidence; target=current takes another photo of the existing organism. Always wait for Start/Identify before capture. Set group for nonbirds. To read an existing result use get_observation; to check progress use get_task_status.",
                {"target": {"type": "string", "enum": ["current", "new"]},
                 "hint": NAME, "group": {"type": "string", "enum": GROUPS}}, ("target",)),
    tool_schema("get_observation", "Read the existing focus evidence/identification without taking a new photo. For progress use get_task_status; for species facts or comparisons use ask_expert."),
    tool_schema("get_task_status", "Read actual photo identification or expert investigation progress without restarting work or explaining results. Use for identification process, status, progress, is it done; select a task when ambiguous.",
                {**TASK_SELECTOR, "scope": {"type": "string", "enum": ["auto", "camera", "investigations", "all"]}}),
    tool_schema("confirm_observation", "Add it / log it / two of them: confirm the focus, not a new named sighting. No speech.", {"count": COUNT, "notes": NAME}),
    tool_schema("discard_observation", "Discard the focus; do not log it."),
    tool_schema("new_observation", "Start identifying a new unknown organism, even when the user does not say new/another. Creates independent evidence and prepares a preview; waits for Start/Identify. Set group for nonbirds. Equivalent to look(target=new).", {"group": {"type": "string", "enum": GROUPS}}),
    tool_schema("describe", "Attach a user description to the focus; resolve only explicit species names.", {"text": NAME, "name": NAME}, ("text",)),
    tool_schema("ask_expert", "Start a background investigation about the current focus: comparisons, conflicting evidence, diet, habitat or behaviour. Returns immediately; never log.", {"question": NAME}, ("question",)),
    tool_schema("get_investigations", "Read investigations or explain a requested result. Prefer get_task_status for progress. Set explain=true ONLY when the user asks to hear/explain a result or accepts the latest explanation offer. Use id (R1 etc.) or first/latest/all.",
                {**TASK_SELECTOR, "explain": {"type": "boolean"}}),
    tool_schema("manage_investigation", "Cancel or retry an investigation. Retry uses its ORIGINAL question and evidence, even after changing focus. Use id or first/latest/all; default latest active for cancel.",
                {**TASK_SELECTOR, "action": {"type": "string", "enum": ["cancel", "retry"]}}, ("action",)),
    tool_schema("accept_suggestions", "Accept pending suggestions only on explicit yes/yeah/sure or an intelligible add command. Never accept on lone Ah/Okay/Uh/Oh/Mm/Mm-hmm, questions, or a notification. Silent.",
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
    tool_schema("ignore", 'Silently ignore chatter, unintelligible fragments, incomplete commands, and lone fillers/acknowledgements (Ah, Okay, Uh, Oh, Mm, Mm-hmm). With pending BirdNET suggestions or an unfinished prior request, never accept or dismiss on them. Exception: Okay/好的 accepts the latest investigation explanation offer via get_investigations(explain=true).'),
]
INSTRUCTIONS = """You are a concise hands-free birding/naturalist companion with an open microphone.
Reply in English by default; the user may speak Chinese: answer in the language of the question.
Classify ONLY the latest user turn. Ordinary off-topic remarks/questions to a companion,
self-talk, phone calls, noise and pet-dog chatter are NOT addressed to you: call ignore.
Named species sighting reports are addressed even without a wake word: call add_observation.
An unknown organism category (animal, plant, flower, insect, etc.) is not an identified
species to log. A user reporting an unknown organism or asking to identify it needs
a new observation/preview, including when an old photo is still in the context.
Multiple species in one report: call add_observations with all items in ONE call.
When the latest assistant question asks to log BirdNET suggestions: yes / yes both / add them all -> accept_suggestions
with omitted species (all pending); just the cardinal -> accept_suggestions with that species.
No / don't add them -> dismiss_suggestions. These actions are silent, not speak.
Only accept explicit user confirmation, never a BirdNET notification itself.
For total count corrections use set_count; for deletion use remove_observation.
Never claim to have logged anything without a successful tool call. Logging has an earcon,
not speech. Never give delayed logging acknowledgements, including "I've logged X".
Choose the operation semantically from the latest request and the supplied state.
Do not require particular command words or repeat an old species for a newly reported subject.
For visual identification call look with an explicit target, NOT speak:
target=new for a newly encountered unknown organism; target=current for another
photo of the same organism. A different group starts independent evidence.
Use get_observation for a question about the existing photo/result without a new
capture; use get_task_status for progress. Existing evidence does not prevent a
new subject or a requested retake. If the referent is genuinely unclear, ask a
short clarifying question with speak; do not guess from an old identification.
For task progress/status, including "Check out the identification process", use
get_task_status. This reads the existing photo/investigation; never call look,
new_observation or ask_expert to check progress. Completion is still an existing
task, including before its result is spoken. Only an explicit request for a new
photo, look again, retake or different subject may restart photo preparation.
If the latest camera photo is still being captured/analyzed, use get_task_status
for its status. Only a requested retake or new subject may replace that capture.
Look prepares the camera; it never takes a photo until the camera is ready and
the user explicitly says start/开始 or taps Identify. Never claim a photo has
already been taken while camera confirmation is pending.
Use the current focus snapshot for "add it", "two of them", "add them", or "log that":
call confirm_observation with the count, NEVER add_observation/add_observations.
This preserves the focus's existing photo evidence; do not create a named sighting
from a pronoun-based confirmation. Named, independent sightings still use add_observation.
Pronouns refer to the focus. For species comparisons ('could it be a Cooper's
Hawk?'), uncertain identification, diet ('what does it eat?'), habitat or behaviour
call ask_expert with the complete latest question, NOT speak or get_observation.
get_observation only reads evidence; it does not supply species facts.
ask_expert schedules work; its immediate answer is an acknowledgement, not research findings.
While investigations run, keep handling new questions and sightings normally.
"How is the first investigation going?", "Any results?", "刚才查得怎么样？"
use get_task_status, NOT ask_expert. Select the requested R-number or first/latest/all;
use scope=investigations for explicit research, camera for photos, auto if unspecified.
Progress/status questions never read a finished result automatically.
"Explain R1", "Tell me the result", "讲一下第一个结果" use get_investigations(explain=true).
After an investigation-ready offer, "yes", "okay", "go ahead", "好的", "好的，讲一下" accept that
explanation with get_investigations(explain=true), NOT ask_expert or accept_suggestions.
"Not now" / "先不讲" after that offer uses ignore, NOT dismiss_suggestions.
If several results were offered and no one is selected, ask which result to explain.
Completed investigations are only offered at a conversational pause; never read
their findings without the user's request or acceptance.
"Cancel the first investigation" / "取消第一个调查" uses manage_investigation(action=cancel).
"Try R1 again" / "继续查第一个" uses manage_investigation(action=retry).
An explicit request to start another investigation uses ask_expert, not a status read.
Never invent progress, percentages, results or completion times.
For a newly encountered organism call new_observation or look(target=new), even if
an earlier focus exists. This includes ordinary reports with an indefinite noun,
without the words new/another. Set the appropriate group, such as Plantae for plants.
"Look again" keeps that focus with look(target=current). Discard unwanted IDs with discard_observation.
Prepare the preview and wait for Start/Identify before photographing it.
Never invent sightings or identify from insufficient evidence; request camera evidence with look.
Questions about birds heard, sounds, calls, or 'anything else' use recent_sounds(seconds=15). Only explicit questions about logged sightings/counts use get_checklist.
Greetings, mic checks and general questions addressed to you use speak; sound/checklist questions use their read tools.
For local ID candidates use nearby_species.
Filler-only speech and unintelligible fragments (including "As for you, for me.") use ignore.
Your selected tool must match your reason: an input you decide to ignore uses
ignore, never speak. Ambiguous fragments/ordinals never authorize checklist writes.
Questions such as "Did you hear them?" never approve pending suggestions: use recent_sounds.
"""
ROUTING = INSTRUCTIONS + """
ROUTING PHASE ONLY: select exactly ONE function call with NO text or audio.
Chatter -> ignore; named species sighting -> add_observation; unknown subject ->
new_observation/look(target=new); addressed question -> the appropriate tool.
Earlier tool outputs are already handled. Do not invent sightings from questions.
[TAP] means the next utterance is addressed to you.
"""
ROUTING = 'HIGHEST-PRIORITY ROUTING RULE: If the latest assistant question is an investigation explanation offer, Okay/好的 explicitly accepts that offer: use get_investigations(explain=true), never accept_suggestions. Otherwise a turn consisting only of ah, okay, uh, oh, mm, or mm-hmm is ignore, even immediately after a pending-suggestion question or an unfinished earlier request. These fillers are NOT approval or dismissal. Yes/yeah/sure or an intelligible add command can approve BirdNET suggestions. Apply this before choosing any other tool.' + "\n" + ROUTING
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
    def __new__(cls, text, item_ids=(), acknowledged=False, context=None):
        obj = super().__new__(cls, text)
        obj.item_ids = set(item_ids)
        obj.acknowledged = acknowledged
        obj.context = context
        return obj


class Session:
    transport = "websocket"

    def __init__(self, session_id, realtime=None):
        self.id = session_id
        self.realtime = realtime or RealtimeConfig.load()
        self.clients = set()
        self.mic = None
        self.observations = {}
        self.focus = None
        self.frames = {}
        self.camera_request = None
        self.camera_feedback_request = None
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
        self.turn_question = ""
        self.input_acknowledged = False
        self.turn_context = None
        self.turn_revision = None
        self.generation = None
        self.cancel_barrier = None
        self.update_expected = None
        self.vision_answered = set()
        self.last_assistant_text = ""
        self.expert_readout = None
        self.investigation_offer_ids = []
        self.task_status_offer = None
        self.first_audio = False
        self.audio_playback_id = None
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
        self.input_audio_turns = deque()
        self.current_input_audio_turn = None
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
        self.auto_speech_paused = False
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
        self.diagnostic_tasks = set()
        self.transcripts = {}
        self.total_audio_in = 0
        self.total_audio_out = 0
        self.expert = NaturalistExpert()
        self.input_revision = 0
        self.investigations = InvestigationQueue(self.run_investigation,
            lambda event: self.phone_json(event), self.investigation_finished)

    @staticmethod
    def chinese(text):
        return any("\u3400" <= c <= "\u9fff" for c in text)

    async def run_investigation(self, question, snapshot, progress):
        return await self.expert.answer(question, snapshot,
            timeout=self.investigations.settings.request_timeout, on_progress=progress)

    async def ask_expert(self, question, obs, *, origin="question"):
        if not self.expert.available:
            from expert import fallback
            return fallback(question, "not_configured")
        try:
            job = await self.investigations.submit(question, obs.payload(), origin=origin)
        except QueueFull:
            return {"ok": False, "status": "queue_full", "answer":
                "调查队列已满，请取消一项或等一项完成。" if self.chinese(question) else
                "The investigation queue is full; cancel a task or wait for one to finish."}
        answer = f"调查 {job.id} 已安排。" if self.chinese(question) else f"Investigation {job.id} is scheduled."
        return {"ok": True, "status": "scheduled", "investigation_id": job.id,
                "answer": answer, "investigation": job.payload()}

    def investigation_answer(self, job, *, chinese=None, include_result=True):
        zh = self.chinese(job.question) if chinese is None else chinese
        target = job.payload()["species"]
        prefix = (f"调查 {job.id}" + (f"（关于 {target}）" if target else "") if zh else
                  f"Investigation {job.id}" + (f", about {target}," if target else ""))
        if job.status == "completed" and include_result:
            return prefix + ("：" if zh else ": ") + job.result["answer"]
        statuses = {"queued": ("正在排队", "is queued"),
                    "running": (("正在检索资料" if job.stage == "retrieving" else "正在分析证据"),
                                ("is retrieving sources" if job.stage == "retrieving" else "is analyzing evidence")),
                    "completed": ("已完成", "is complete"), "cancelled": ("已取消", "is cancelled"),
                    "timed_out": ("达到本次时限，已保留问题和证据，可以重新调查", "reached its time limit; its question and evidence are saved for a retry"),
                    "failed": ("暂时未能完成，问题和证据已保留，可以重试", "could not finish; its question and evidence are saved for a retry")}
        words = statuses[job.status][0 if zh else 1]
        if job.status == "queued":
            queued = [j.id for j in self.investigations.jobs.values() if j.status == "queued"]
            position = queued.index(job.id) + 1
            words += f"，排第 {position} 位" if zh else f", position {position}"
        return prefix + ("" if zh else " ") + words + ("。" if zh else ".")

    async def investigation_finished(self, job):
        result = job.result or {}
        obs = self.observations.get(job.observation["id"])
        # Ordinary conversation never invalidates research. A capture hint may
        # update only the exact, still-current observation snapshot it describes.
        if (job.status == "completed" and result.get("action") == "request_more_evidence"
                and result.get("hint") and obs and obs.status == "open"
                and self.focus == obs.id and expert_revision(obs.payload()) == expert_revision(job.observation)):
            obs.hint = result["hint"]
            await self.broadcast_observation(obs)
        if job.status in ("completed", "failed", "timed_out"):
            self.pending.append(("investigation", job.id, None))
            await self.flush_pending()
        self.log("investigation_done", id=job.id, status=job.status)

    def investigation_offer(self, jobs, *, chinese):
        ready = [j.id for j in jobs if j.status == "completed"]
        failed = [j.id for j in jobs if j.status != "completed"]
        parts = []
        if ready:
            ids = ("、" if chinese else ", ").join(ready)
            parts.append((f"另外，调查 {ids} 已完成。" if chinese else
                          f"Also, investigation{'s' if len(ready) > 1 else ''} {ids} "
                          + ("are ready. " if len(ready) > 1 else "is ready. "))
                         + (("你想先听哪一项？" if len(ready) > 1 else "要我讲一下结果吗？") if chinese else
                            ("Which would you like me to explain?" if len(ready) > 1 else
                             "Would you like me to explain?")))
        if failed:
            ids = ("、" if chinese else ", ").join(failed)
            parts.append(f"调查 {ids} 暂未完成，详情已保存在卡片里。" if chinese else
                         f"Investigation {ids} could not finish; details are on the task cards.")
        return " ".join(parts)

    async def investigation_notice(self, job):
        if job.announced or job.status not in ("completed", "failed", "timed_out"):
            return
        # Combine completions already waiting at this conversational boundary.
        batch = [job]
        for pending in list(self.pending):
            if pending[0] != "investigation" or (pending[2] is not None and pending[2] > time.monotonic()):
                continue
            other = self.investigations.jobs.get(pending[1])
            if other and not other.announced and other not in batch and other.status in ("completed", "failed", "timed_out"):
                batch.append(other)
                self.pending.remove(pending)
        revision = self.input_revision
        def uninterrupted():
            return (not self.closed and not self.quiet and not self.user_speaking
                    and not self.awaiting_transcript and self.input_revision == revision
                    and not self.playback_pending() and time.monotonic() >= self.play_until)
        async def defer():
            for item in batch:
                item.announced = False
                if not self.closed and not any(p[:2] == ("investigation", item.id) for p in self.pending):
                    self.pending.append(("investigation", item.id, None))
            await self.investigations.emit()
        if not uninterrupted():
            await defer()
            return
        for item in batch:
            item.announced = True  # The short offer is separate from reading the result.
        await self.investigations.emit()
        if not uninterrupted():
            await defer()
            return
        answer = self.investigation_offer(batch, chinese=self.lang.startswith("zh") or self.chinese(batch[0].question))
        previous_offer = self.investigation_offer_ids
        self.investigation_offer_ids = [j.id for j in batch if j.status == "completed"]
        try:
            for item in batch:
                item.notice_attempts += 1
            # Do not put findings in this voice turn. The user must request them.
            await self.inject("[INVESTIGATION_READY] " + json.dumps({
                "ids": [j.id for j in batch], "answer": answer,
                "instruction": "Offer to explain; do not read any investigation findings."}, ensure_ascii=False))
            if not uninterrupted():
                self.investigation_offer_ids = previous_offer
                await defer()
                return
            response = await self.speak_expert(answer, guard=uninterrupted)
            if response and response.get("status") == "deferred":
                self.investigation_offer_ids = previous_offer
                await defer()
        except Exception:
            self.investigation_offer_ids = previous_offer
            for item in batch:
                item.announced = False
                if item.notice_attempts < 2:
                    self.pending.append(("investigation", item.id, time.monotonic() + 5))
            await self.investigations.emit()
            raise

    async def investigation_tool(self, name, args):
        zh = self.chinese(self.turn_question) if self.turn_question else self.lang.startswith("zh")
        if name == "get_investigations":
            explain = args.get("explain", False)
            if not isinstance(explain, bool):
                raise ValueError("explain must be boolean")
            jobs = self.investigations.select(args.get("id"), args.get("selection"))
            if not args.get("id") and not args.get("selection"):
                if explain:
                    offered = [j for j in jobs if j.id in self.investigation_offer_ids and j.status == "completed"]
                    jobs = offered or [j for j in jobs if j.status == "completed"][-1:] or jobs[-1:]
                    if len(jobs) > 1:
                        return {"ok": True, "answer": self.investigation_offer(jobs, chinese=zh), "requires_selection": True}
                else:
                    jobs = [j for j in jobs if j.status in ACTIVE] or jobs[-1:]
            if not jobs:
                answer = "目前没有调查任务。" if zh else "There are no investigations yet."
            else:
                answer = " ".join(self.investigation_answer(j, chinese=zh, include_result=explain) for j in jobs[:3])
                ready = [j for j in jobs[:3] if j.status == "completed"]
                if ready and not explain:
                    answer += " 要我讲一下结果吗？" if zh else " Would you like me to explain?"
                if len(jobs) > 3:
                    answer += f" 另有 {len(jobs) - 3} 项可在屏幕查看。" if zh else f" There are {len(jobs) - 3} more on screen."
                if self.may_speak():
                    self.investigation_offer_ids = [] if explain else [j.id for j in ready]
                    for job in jobs[:3]:
                        if job.status not in ACTIVE:
                            job.announced = True
                    await self.investigations.emit()
            return {"ok": True, "answer": answer, "investigations": [
                {**j.payload(), **({} if explain else {"result": None})} for j in jobs]}
        action = args.get("action")
        if action not in ("cancel", "retry"):
            raise ValueError("Investigation action must be cancel or retry")
        jobs = self.investigations.select(args.get("id"), args.get("selection") or "latest",
            active=action == "cancel" and not args.get("id") and not args.get("selection"))
        if not jobs:
            return {"ok": False, "answer": "没有匹配的调查任务。" if zh else "There is no matching investigation."}
        if action == "cancel":
            cancelled = [j.id for j in jobs if await self.investigations.cancel(j)]
            answer = (("已取消调查 " + "、".join(cancelled) + "。") if zh else
                      "Cancelled investigation " + ", ".join(cancelled) + ".") if cancelled else (
                      "这些调查已结束。" if zh else "Those investigations have already ended.")
            return {"ok": True, "answer": answer, "cancelled": cancelled}
        if len(jobs) != 1:
            raise ValueError("Retry one investigation at a time")
        old = jobs[0]
        if old.status in ACTIVE:
            return {"ok": True, "answer": self.investigation_answer(old, chinese=zh)}
        if not self.expert.available:
            from expert import fallback
            return fallback(self.turn_question or old.question, "not_configured")
        job = await self.investigations.submit(old.question, old.observation, parent_id=old.id)
        return {"ok": True, "investigation_id": job.id,
                "answer": f"调查 {job.id} 已安排。" if zh else f"Investigation {job.id} is scheduled."}

    async def speak_expert(self, answer, *, guard=None):
        # Keep metadata/hints out of the speech task; a general answer prompt
        # otherwise encourages Higgs to elaborate or translate the decision.
        self.expert_readout = answer
        try:
            return await self.generate("answer", guard=guard) if guard else await self.generate("answer")
        finally:
            self.expert_readout = None

    def task_status_snapshot(self):
        """Read canonical work state; never copy findings or change task ownership."""
        queued = [j.id for j in self.investigations.jobs.values() if j.status == "queued"]
        rows = [{"id": j.id, "kind": "investigation", "observation_id": j.observation["id"],
                 "status": j.status, "stage": j.stage, "created_at": j.created_at,
                 "queue_position": queued.index(j.id) + 1 if j.id in queued else 0}
                for j in self.investigations.jobs.values()]
        obs = self.observations.get(self.focus)
        if obs and obs.status != "discarded" and obs.analysis_request_id:
            stage = obs.analysis_stage
            if stage == "idle":  # Compatibility with observations made by older clients/tests.
                stage = {"awaiting_confirmation": "awaiting_confirmation", "analyzing": "recognizing"}.get(
                    obs.analysis_status, obs.analysis_status)
            status = {"awaiting_confirmation": "waiting", "capturing": "running",
                      "analyzing": "running", "ready": "completed"}.get(obs.analysis_status, obs.analysis_status)
            linked = [j for j in self.investigations.jobs.values() if j.origin == "vision"
                      and j.observation["id"] == obs.id
                      and j.observation.get("image_revision") == obs.image_revision
                      and j.observation.get("analysis_request_id") == obs.analysis_request_id]
            pending = (obs.id, obs.image_revision) not in self.vision_answered
            if obs.analysis_status == "ready":
                if linked:
                    job = linked[-1]
                    stage, status = "expert_" + job.stage, job.status
                    pending = pending or (job.status == "completed" and not job.announced)
                elif pending and obs.uncertain and self.expert.available and any(
                        p[0] == "vision" and isinstance(p[1], dict) and p[1].get("obs") == obs.id
                        and p[1].get("image_revision") == obs.image_revision for p in self.pending):
                    stage, status = "awaiting_expert", "queued"
            finished = obs.analysis_finished or time.monotonic()
            rows.append({"id": "photo:" + obs.analysis_request_id, "kind": "camera",
                         "observation_id": obs.id, "request_id": obs.analysis_request_id,
                         "image_revision": obs.image_revision, "status": status, "stage": stage,
                         "created_at": obs.analysis_created_at or obs.t_start,
                         "elapsed_seconds": round(max(0, finished - obs.analysis_started), 1)
                             if obs.analysis_started is not None else None,
                         "pending_result": pending if obs.analysis_status == "ready" else False,
                         "queue_position": queued.index(linked[-1].id) + 1 if linked and linked[-1].id in queued else 0,
                         "related_investigation_ids": [j.id for j in linked]})
        return sorted(rows, key=lambda r: datetime.fromisoformat(r["created_at"]).timestamp())

    @staticmethod
    def task_status_sentence(row, zh=False):
        if row["kind"] == "investigation":
            words = {"queued": ("正在排队", "is queued"), "retrieving": ("正在检索资料", "is retrieving sources"),
                     "analyzing": ("正在分析证据", "is analyzing evidence"),
                     "completed": ("已完成，结果已就绪", "is complete; its result is ready"),
                     "failed": ("未能完成，可以重试", "could not finish; it can be retried"),
                     "timed_out": ("已达到时限，可以重试", "reached its time limit; it can be retried"),
                     "cancelled": ("已取消", "is cancelled")}
            stage = row["stage"] if row["stage"] in words else row["status"]
            label = words.get(stage, ("正在处理", "is running"))[0 if zh else 1]
            if stage == "queued":
                label += (f"，排第 {row.get('queue_position', 0)} 位" if zh else
                          f", position {row.get('queue_position', 0)}")
            return f"调查 {row['id']}{label}。" if zh else f"Investigation {row['id']} {label}."
        stage = row["stage"]
        if stage.startswith("expert_"):
            expert = {"kind": "investigation", "id": row["related_investigation_ids"][-1],
                      "status": row["status"], "stage": stage[7:], "queue_position": row.get("queue_position", 0)}
            return ("图片识别已完成；" if zh else "Image recognition is complete; ") + Session.task_status_sentence(expert, zh)
        words = {
            "camera_opening": ("相机正在打开，还没有拍照。", "The camera is opening; no photo has been taken yet."),
            "awaiting_confirmation": ("相机已就绪，正在等你说 Start 或点击 Identify 后拍照。", "The camera is ready, waiting for Start or Identify before taking a photo."),
            "capturing": ("正在拍摄并上传照片。", "The camera is taking and uploading the photo."),
            "range_lookup": ("照片已收到，正在查询本地可能出现的物种。", "The photo has been received; I'm checking local species."),
            "recognizing": ("照片已收到，图像模型正在识别。", "The photo has been received; the image model is identifying the subject."),
            "resolving_species": ("图像识别已结束，正在整理候选物种资料。", "Image recognition has finished; I'm matching the candidates with species information."),
            "fusing": ("正在综合图片、地点和声音证据。", "I'm combining the image results with local and sound evidence."),
            "awaiting_expert": ("图片识别已完成，专家分析正在等待启动。", "Image recognition is complete; expert analysis is waiting to start."),
            "ready": ("照片识别已完成，结果已就绪。", "Photo identification is complete; the result is ready."),
            "failed": ("照片识别未能完成，可以重新尝试。", "Photo identification could not finish; you can request a retry."),
            "cancelled": ("照片识别已取消。", "Photo identification was cancelled.")}
        return words.get(stage, ("照片任务正在处理。", "The photo task is being processed."))[0 if zh else 1]

    async def task_status(self, args):
        scope = args.get("scope", "auto")
        selection = args.get("selection")
        if scope not in ("auto", "camera", "investigations", "all") or selection not in (None, "first", "latest", "all"):
            raise ValueError("Invalid task selection")
        rows = self.task_status_snapshot()
        task_id = str(args.get("id") or "").upper()
        if task_id:
            rows = [r for r in rows if r["id"].upper() == task_id]
        elif scope == "camera":
            rows = [r for r in rows if r["kind"] == "camera"]
        elif scope == "investigations":
            rows = [r for r in rows if r["kind"] == "investigation"]
        elif scope == "auto":
            linked = {i for r in rows if r["kind"] == "camera" for i in r["related_investigation_ids"]}
            rows = [r for r in rows if r["id"] not in linked]
            if selection is None:
                active = [r for r in rows if r["status"] in ("running", "queued", "waiting")]
                rows = active or rows[-1:]
        if selection == "first":
            rows = rows[:1]
        elif selection == "latest":
            rows = rows[-1:]
        zh = self.chinese(self.turn_question) if self.turn_question else self.lang.startswith("zh")
        if not rows:
            return {"ok": True, "tasks": [], "answer": ("没有匹配的拍照识别或调查任务。" if zh else
                    "There is no matching photo identification or investigation task.")}
        ambiguous = len(rows) > 1 and scope not in ("all",) and selection != "all"
        if ambiguous:
            names = [("照片识别" if zh else "photo identification") if r["kind"] == "camera" else
                     (f"调查 {r['id']}" if zh else f"investigation {r['id']}") for r in rows[:3]]
            answer = ("你想查询哪一项：" + "、".join(names) + "？" if zh else
                      "Which task do you mean: " + ", ".join(names) + "?")
        else:
            answer = " ".join(self.task_status_sentence(r, zh) for r in rows[:3])
            if len(rows) > 3:
                answer += f" 另有 {len(rows) - 3} 项可在屏幕查看。" if zh else f" There are {len(rows) - 3} more tasks."
        return {"ok": True, "answer": answer, "tasks": rows, "requires_selection": ambiguous}

    def task_status_context(self):
        """Expose a live clarification to the semantic router, without parsing speech."""
        offer = self.task_status_offer
        if (offer and time.monotonic() - offer["at"] <= 30
                and self.normalized_words(self.last_assistant_text) == self.normalized_words(offer["question"])):
            return {"question": offer["question"],
                    "tasks": [{"id": r["id"], "kind": r["kind"]} for r in offer["tasks"]]}
        return None

    async def speak_task_status(self, args):
        revision = self.input_revision
        for _ in range(2):
            result = await self.task_status(args)
            if not self.may_speak():
                return result
            fingerprint = lambda: [{k: v for k, v in r.items() if k != "elapsed_seconds"}
                                   for r in self.task_status_snapshot()]
            snapshot = fingerprint()
            # Recheck after the model's setup wait: progress may finish while
            # preparing speech. Re-read instead of speaking an obsolete stage.
            guard = lambda: self.input_revision == revision and fingerprint() == snapshot
            response = await self.speak_expert(result["answer"], guard=guard)
            if self.input_revision != revision or not response or response.get("status") != "deferred":
                if (self.input_revision == revision and response and response.get("status") == "completed"
                        and result.get("requires_selection")):
                    self.task_status_offer = {"question": result["answer"], "tasks": result["tasks"],
                                              "at": time.monotonic()}
                return result
        self.pending.append(("task_status", {"args": dict(args), "revision": revision,
                                            "question": str(self.turn_question)}, None))
        await self.flush_pending()
        return result

    async def answer_vision(self, observation_id):
        expected = observation_id if isinstance(observation_id, dict) else None
        observation_id = expected["obs"] if expected else observation_id
        obs = self.observations.get(observation_id)
        input_revision = self.input_revision
        if not obs or obs.status != "open" or self.focus != obs.id:
            return
        identity = (obs.id, obs.image_revision)
        if (obs.analysis_status not in ("idle", "ready") or (expected and identity in self.vision_answered)
                or (expected and (expected["image_revision"] != obs.image_revision
                                 or expected["request_id"] != obs.analysis_request_id))):
            self.log("stale_vision_answer_skipped", observation_id=obs.id)
            return
        if obs.uncertain and self.expert.available:
            question = "根据现有证据，这是什么？不确定时告诉我还需要拍什么。" if self.lang.startswith("zh") else (
                "What is this organism, based on the evidence? If uncertain, what should I capture next?")
            try:
                result = await self.ask_expert(question, obs, origin="vision")
            except ValueError:
                result = {"ok": False}  # A full research queue preserves basic Look.
            if result.get("ok"):
                self.vision_answered.add(identity)
                def current():
                    return (self.input_revision == input_revision and self.focus == obs.id
                            and obs.status == "open" and obs.analysis_status == "ready"
                            and (obs.id, obs.image_revision) == identity)
                if self.may_speak() and current():
                    # This is a background event, possibly after a progress
                    # answer consumed the latest user input. Give the speech
                    # response its own event instead of re-answering that turn.
                    await self.inject("[VISION_INVESTIGATION_SCHEDULED] " + json.dumps({
                        "observation_id": obs.id, "image_revision": obs.image_revision,
                        "answer": result["answer"]}, ensure_ascii=False))
                    if current():
                        await self.speak_expert(result["answer"], guard=current)
                return
        if (self.focus == obs.id and obs.status == "open" and self.may_speak()
                and self.input_revision == input_revision):
            await self.inject("[VISION] " + json.dumps({"top3": obs.fused[:3], "uncertain": obs.uncertain, "hint": obs.hint}))
            response = await self.generate("answer")
            if response and response.get("status") == "completed" and self.input_revision == input_revision:
                self.vision_answered.add(identity)

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
            await self.cancel_camera_request()
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
        await self.cancel_camera_request()
        self.last_assistant_text = ""
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
            # A confirmed entry stays immutable; another photo opens a record
            # in the same category instead of silently defaulting back to birds.
            obs = await self.new_focus(obs.group if obs else "Aves")
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

    async def cancel_camera_request(self):
        request = self.camera_request
        self.camera_request = None
        self.camera_feedback_request = None
        if request and (obs := self.observations.get(request["obs"])):
            obs.awaiting_answer = False
            if obs.analysis_status != "ready":
                obs.analysis_status = "cancelled"
                obs.analysis_stage = "cancelled"
                obs.analysis_finished = time.monotonic()
        self.pending = deque(p for p in self.pending if p[0] not in ("camera_ready", "camera_start", "camera_photo"))
        if request and self.mic:
            try:
                await self.mic.send_json({"type": "camera_cancelled", "request_id": request["id"]})
            except (ConnectionError, WebSocketDisconnect, RuntimeError):
                pass

    async def request_look(self, hint="", group=None, *, confirmed=False, open_id=None):
        if group is not None and group not in GROUPS:
            raise ValueError("invalid iconic group")
        # A category change is a real evidence boundary, regardless of whether it
        # came from voice, the preview selector or the Identify button.
        active = self.observations.get(self.focus)
        if active and group is not None and active.group != group:
            await self.new_focus(group)
        await self.cancel_camera_request()
        self.pending = deque(p for p in self.pending if p[0] != "vision")
        obs = await self.current_focus(create=True)
        if group is not None:
            obs.group = group
        obs.hint = str(hint or obs.hint)[:500]
        obs.awaiting_answer = False
        obs.analysis_status = "awaiting_confirmation"
        obs.analysis_stage = "camera_opening"
        obs.analysis_started = obs.analysis_finished = None
        obs.analysis_created_at = now_iso()
        obs.touched = time.monotonic()
        request = self.camera_request = {"id": uuid.uuid4().hex, "obs": obs.id,
            "phase": "opening", "confirmed": confirmed, "open_id": open_id,
            "language": "zh" if self.chinese(self.turn_question) or (not self.turn_question and self.lang.startswith("zh")) else "en"}
        obs.analysis_request_id = request["id"]
        await self.broadcast_observation(obs)
        if self.mic:
            await self.mic.send_json({"type": "camera_prepare", "obs": obs.id,
                "request_id": request["id"], "hint": obs.hint, "open_id": open_id})
        return {"ok": True, "observation": obs.payload(), "waiting_for_confirmation": True}

    async def photo_stage(self, obs, request, stage):
        if self.camera_request is not request or self.focus != obs.id or obs.status != "open":
            return False
        obs.analysis_stage = stage
        self.log("photo_stage", request_id=request["id"], observation_id=obs.id, stage=stage)
        await self.broadcast_observation(obs)
        return self.camera_request is request and self.focus == obs.id and obs.status == "open"

    async def photo_failed(self, obs, request):
        if self.camera_request is request and self.focus == obs.id and obs.status == "open":
            request["phase"], request["confirmed"] = "ready", False
            obs.awaiting_answer = False
            obs.analysis_status = obs.analysis_stage = "failed"
            obs.analysis_finished = time.monotonic()
            await self.broadcast_observation(obs)
        if self.camera_feedback_request is request:
            self.camera_feedback_request = None
            self.pending = deque(p for p in self.pending if p[0] != "camera_photo")

    async def camera_ready(self, event):
        request = self.camera_request
        if not event.get("request_id"):
            # A manually opened preview follows the same confirmation handshake.
            if not isinstance(event.get("open_id"), str) or not 0 < len(event["open_id"]) <= 128:
                raise ValueError("camera_ready requires a preview id")
            await self.request_look(group=event.get("group"), open_id=event.get("open_id"))
            return
        if not request or event["request_id"] != request["id"] or request["phase"] != "opening":
            return
        request["phase"] = "ready"
        if obs := self.observations.get(request["obs"]):
            obs.analysis_stage = "awaiting_confirmation"
            await self.broadcast_observation(obs)
        if self.camera_request is not request:
            return
        if request["confirmed"]:
            await self.queue_camera_start(request)
        else:
            self.pending.append(("camera_ready", request["id"], None))
            await self.flush_pending()

    async def queue_camera_start(self, request):
        if self.camera_request is not request or request["phase"] != "ready":
            return
        request["phase"], request["confirmed"] = "starting", True
        self.log("camera_confirmed", request_id=request["id"])
        self.pending = deque(p for p in self.pending if p[0] != "camera_ready")
        request["start_acknowledged"] = bool(await self.play_feedback("camera-taking",
            language=request["language"],
            guard=lambda: self.camera_request is request and request["phase"] == "starting"))
        if self.camera_request is not request:
            return
        # This path only sends local feedback/capture commands. It must not sit
        # behind a Higgs response or another tool in the conversation worker.
        await self.start_camera_capture(request["id"])

    async def camera_confirmation(self, text):
        request = self.camera_request
        if not request:
            return False
        words = " ".join(self.normalized_words(text))
        if words in ("cancel", "never mind", "not now", "取 消", "先 不 拍", "算 了", "不 要 拍"):
            await self.cancel_camera_request()
            return True
        photo_command = re.fullmatch(
            r"(?:(?:can|could|would|will) you )?(?:please )?(?:"
            r"(?:take|capture|shoot) (?:(?:a|the) )?(?:photo|picture)|photograph (?:it|this|that))"
            r"(?: for me)?(?: please)?", words)
        if not photo_command and words not in ("start", "start please", "take a photo", "take the photo",
                                               "开 始", "开 始 吧", "拍 吧", "开 始 拍 照", "可 以 拍 了"):
            return False
        obs = self.observations.get(request["obs"])
        if not obs or obs.status != "open" or self.focus != obs.id:
            await self.cancel_camera_request()
            return True
        if request["phase"] == "ready":
            if self.assistant_audio_pending():
                await self.cancel_speech()
            await self.queue_camera_start(request)
        else:
            # Recognized camera commands report the actual phase; they do not
            # acknowledge arbitrary input or save early consent for later.
            phase = request["phase"]
            kind = ("camera-opening" if phase == "opening" else
                    "photo-processing" if phase == "classifying" else "camera-taking")
            await self.play_feedback(kind, language=request["language"],
                guard=lambda: self.camera_request is request and request["phase"] == phase)
        # Early/duplicate confirmations never authorize a later capture.
        return True

    async def start_camera_capture(self, request_id):
        request = self.camera_request
        if not request or request["id"] != request_id or request["phase"] != "starting":
            return
        obs = self.observations.get(request["obs"])
        if not obs or obs.status != "open" or self.focus != obs.id:
            await self.cancel_camera_request()
            return
        obs.touched = time.monotonic()
        obs.awaiting_answer = True
        obs.analysis_status = "capturing"
        obs.analysis_stage = "capturing"
        obs.analysis_started = time.monotonic()
        obs.analysis_finished = None
        if not request.get("start_acknowledged"):
            await self.play_feedback("camera-taking", language=request["language"],
                guard=lambda: self.camera_request is request and request["phase"] == "starting")
        known = {d.id for d in obs.detections}
        for stamp, detection in (self.sound_windows if obs.group == "Aves" else ()):
            if abs(time.monotonic() - stamp) <= 10 and detection.id not in known:
                obs.detections.append(detection)
        # Camera capture must not wait for the first BirdNET/range-model load.
        # Sound evidence is fused with the photo after its upload is accepted.
        if self.camera_request is request and self.focus == obs.id and obs.status == "open" and self.mic:
            request["phase"] = "capturing"
            await self.broadcast_observation(obs)
            await self.mic.send_json({"type": "capture", "obs": obs.id, "n": 3,
                "hint": obs.hint, "request_id": request_id})
            self.log("camera_capture_requested", request_id=request_id)

    def camera_photo_current(self, request):
        obs = self.observations.get(request["obs"])
        return (self.camera_feedback_request is request and obs is not None
                and obs.status == "open" and self.focus == obs.id)

    async def announce_camera_photo(self, request_id):
        request = self.camera_feedback_request
        if not request or request["id"] != request_id or not self.camera_photo_current(request):
            return
        played = await self.play_feedback("photo-ready", language=request["language"],
            guard=lambda: self.camera_photo_current(request))
        if self.camera_photo_current(request):
            if played or not self.may_speak():
                self.camera_feedback_request = None
            else:
                self.pending.append(("camera_photo", request_id, None))

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
        client = self.mic
        if client is not None and getattr(client, "diagnostics", False) and len(self.diagnostic_tasks) < 100:
            # No text, SDP, credentials, tool arguments, GPS or provider errors.
            allowed = {"response_id", "item_id", "phase", "reason", "confirmation", "hold_ms",
                       "active", "role", "kind", "status", "name", "ok", "transport", "enabled"}
            safe = {k: v for k, v in fields.items() if k in allowed and isinstance(v, (str, bool, int, float, type(None)))}
            async def deliver():
                try:
                    await client.send_json({"type": "diagnostic", "event": event,
                        "server_monotonic": time.monotonic(), "fields": safe})
                except Exception:
                    pass  # Recording must never break conversation/playback.
            task = asyncio.create_task(deliver())
            self.diagnostic_tasks.add(task)
            task.add_done_callback(self.diagnostic_tasks.discard)

    async def mic_attached(self, client):
        pass

    async def mic_detached(self, client):
        pass

    def environment_contaminated(self):
        return self.speaker_mode and self.echo_blocked()

    async def voice_started(self, record):
        if self.echo_guard:
            self.log("barge_in_candidate", confirmation="transcript")
        else:
            await self.cancel_speech()
            self.log("barge_in", confirmation="vad")

    async def phone_json(self, event):
        await asyncio.gather(*(c.send_json(event) for c in list(self.clients)), return_exceptions=True)

    async def phone_audio(self, pcm, *, guard=None):
        await asyncio.gather(*(c.send_audio(pcm, guard=guard) for c in list(self.clients)
                               if c.role == "mic" or c.audio), return_exceptions=True)

    async def acknowledge_tool(self, name):
        """Confirm only a routed, allowed task, before executing its tool."""
        kind = FEEDBACK_TOOLS.get(name)
        if name in ("get_checklist", "recent_sounds", "get_observation"):
            kind = "received"
        if not kind or self.input_acknowledged:
            return False
        revision = self.turn_revision
        def current():
            return ((revision is None or self.input_revision == revision)
                    and not getattr(self, "turn_stale", False)
                    and (self.turn_stopped_at is None or time.monotonic() - self.turn_stopped_at <= 12))
        if not current():
            return False
        played = bool(await self.play_feedback(kind, guard=current))
        self.input_acknowledged = self.input_acknowledged or played
        if played:
            self.log("task_acknowledged", tool=name, kind=kind, revision=revision)
        return played

    async def play_feedback(self, kind, *, language=None, guard=None):
        revision = self.input_revision
        def current():
            local = kind not in ("look", "check") and self.mic is not None
            return (not self.closed and (self.connected or local) and self.may_speak()
                    and not self.auto_speech_paused and not self.user_speaking
                    and not self.awaiting_transcript and self.input_revision == revision
                    and (guard is None or guard()))
        if not kind or not current() or self.playback_pending() or time.monotonic() < self.play_until:
            return
        language = language or ("zh" if self.chinese(self.turn_question) or (not self.turn_question and self.lang.startswith("zh")) else "en")
        try:
            text, pcm = feedback_recording(kind, language)
        except (OSError, ValueError) as exc:
            self.log("tool_feedback_unavailable", error_type=type(exc).__name__)
            return  # Feedback failure must not prevent the requested tool.
        if not current():
            return
        playback_id = "feedback-" + uuid.uuid4().hex
        await self.begin_audio(playback_id)
        def audible():
            return current() and self.audio_playback_id == playback_id
        if not audible():
            if self.audio_playback_id == playback_id:
                await self.flush_audio()
            return
        self.play_until = max(time.monotonic(), self.play_until) + len(pcm) / 48000
        self.echo_estimate = self.play_until
        self.remember_spoken((playback_id, "feedback", kind), text)
        if kind != "received":
            self.last_assistant_text = text
        await self.phone_json({"type": "transcript", "role": "assistant", "text": text, "final": True})
        await self.phone_audio(pcm, guard=audible)
        played = False
        if audible():
            played = True
            self.total_audio_out += len(pcm)
            await self.end_audio()
            self.latency("tool_feedback")
            self.log("feedback_played", kind=kind, revision=revision,
                     request_id=self.camera_request["id"] if self.camera_request else None)
        elif self.audio_playback_id == playback_id:
            await self.flush_audio()
        if not played:
            for row in list(self.spoken):
                if row[0] == (playback_id, "feedback", kind):
                    self.spoken.remove(row)
        if self.playback_task is None or self.playback_task.done():
            self.playback_task = asyncio.create_task(self.playback_finished())
        await self.state()
        return played

    @property
    def speaker_mode(self):
        mic = self.mic or next((c for c in self.clients if c.role == "mic"), None)
        return mic is None or not mic.earphones

    @property
    def echo_guard(self):
        return self.speaker_mode or any(c.role == "display" and c.audio for c in self.clients)

    def playback_clients(self):
        return [c for c in self.clients if c.role == "mic" or c.audio]

    def playback_pending(self):
        return any(c.playback_active or c.playback_pending for c in self.playback_clients())

    async def begin_audio(self, response_id):
        self.audio_playback_id = response_id or "audio-" + uuid.uuid4().hex
        for client in self.playback_clients():
            if client.playback_protocol == 1:
                client.playback_id = self.audio_playback_id
                client.playback_pending = True
        await self.phone_json({"type": "audio_start", "id": self.audio_playback_id})

    async def end_audio(self):
        if self.audio_playback_id:
            await self.phone_json({"type": "audio_end", "id": self.audio_playback_id})

    async def flush_audio(self):
        self.audio_playback_id = None
        self.play_until = 0
        self.echo_estimate = min(self.echo_estimate, time.monotonic())
        for client in self.playback_clients():
            client.playback_id = None
            client.playback_pending = client.playback_active = False
            client.playback_end = time.monotonic()
        await self.phone_json({"type": "flush"})

    def echo_deadline(self):
        clients = self.playback_clients()
        # A start without an end keeps the gate closed, including suspended WebAudio.
        if any(c.playback_active or c.playback_pending for c in clients):
            return float("inf")
        estimate = max(self.echo_estimate, self.play_until)
        return max((max(estimate, c.playback_end) + (.5 if c.playback_end else .8)
                    for c in clients), default=estimate + .8)

    def prune_input_audio_turns(self):
        cutoff = time.monotonic() - 30
        while self.input_audio_turns and (len(self.input_audio_turns) > 32
                or self.input_audio_turns[0]["at"] < cutoff):
            old = self.input_audio_turns.popleft()
            if self.current_input_audio_turn is old:
                self.current_input_audio_turn = None
                self.user_speaking = self.awaiting_transcript = False

    def match_input_audio_turn(self, event, *, final=False):
        self.prune_input_audio_turns()
        # This preview can reuse item_id after clear. Match occurrences in FIFO
        # order, assuming finals for one id preserve its VAD-turn ordering.
        record = next((r for r in self.input_audio_turns
                       if r["id"] == event.get("item_id")
                       and (final or r["stopped"] is None)), None)
        if record is not None and final:
            self.input_audio_turns.remove(record)
        return record

    async def finish_input_audio(self, event):
        record = self.match_input_audio_turn(event, final=True)
        blocked = (record is not None and record["blocked"]) or self.echo_blocked()
        current = self.current_input_audio_turn
        owns_input = record is current if record is not None else current is None
        if owns_input:
            self.current_input_audio_turn = None
            self.user_speaking = self.awaiting_transcript = False
        newer_input = (current is not None and record is not current
                       and (self.user_speaking or self.awaiting_transcript))
        # This preview retains uncommitted input when auto-response is disabled.
        # Clear completed/failed audio without erasing a newer accepted turn.
        if not newer_input:
            await self.rt_send({"type": "input_audio_buffer.clear"})
        if blocked:
            self.log("input_audio_guarded", event_type=event.get("type"),
                     item_id=event.get("item_id"), reason="playback_turn" if record and record["blocked"] else "playback")
            if event.get("item_id"):
                reused = any(r["id"] == event["item_id"] for r in self.input_audio_turns)
                reused |= (self.current_input_audio_turn is not None
                           and self.current_input_audio_turn["id"] == event["item_id"])
                if reused:
                    # The provider id may now refer to the newer audio item.
                    self.log("input_audio_delete_skipped", item_id=event["item_id"], reason="reused_item_id")
                else:
                    await self.delete_items([event["item_id"]])
            await self.flush_pending()
            return False
        return True

    @staticmethod
    def normalized_words(text):
        # Chinese speech has no word spaces. Keep characters as tokens so long
        # Chinese playback echoes can be compared just like English word runs.
        return re.findall(r"[\u3400-\u9fff]|[^\W_\u3400-\u9fff]+", text.lower())

    @staticmethod
    def stop_request(text):
        command = "".join(c.lower() for c in text if c.isalnum())
        commands = {"stop", "stoptalking", "stopspeaking", "pleasestop", "stopplease",
            "pause", "pauseplease", "pleasepause", "wait", "holdon", "onesecond",
            "停", "停下", "停一下", "先停一下", "别说了", "先别说了", "不要说了",
            "等等", "等一下", "等会儿", "暂停", "暂停一下"}
        return any(command == phrase * repeat for phrase in commands for repeat in (1, 2, 3))

    def assistant_audio_pending(self):
        return (self.playback_pending() or time.monotonic() < self.play_until
                or (self.active and self.phase == "answer"))

    @staticmethod
    def filler_only(text):
        words = re.sub(r"[^\w\s]", "", str(text).lower()).split()
        return bool(words) and all(w in {"ah", "uh", "um", "oh", "hmm", "mm", "mmhmm", "mhm", "er", "erm"} for w in words)

    def consent_context(self):
        return {
            "log_offer": bool(re.search(r"(?:add|log|record|加入|记录).*(?:\?|？)$", self.last_assistant_text, re.I)),
            "explanation_offer": bool(self.investigation_offer_ids and re.search(
                r"(?:would you like me to explain|which would you like me to explain|要我讲一下结果吗|你想先听哪一项)",
                self.last_assistant_text, re.I))}

    def ignored_input_reason(self, text):
        """Apply known no-intent guards before audio cancellation, not just tools."""
        if not text.strip() or self.filler_only(text):
            return "empty_or_filler"
        context = self.consent_context()
        name = ("get_investigations" if context["explanation_offer"] else
                "confirm_observation" if context["log_offer"] else "speak")
        decision, reason = route_guard(name, {"explain": True}, text,
            suggestions=bool(self.suggestions), **context)
        return reason if decision == "ignore" else None

    def remember_spoken(self, key, text):
        if not text.strip():
            return
        for row in list(self.spoken):
            if row[0] == key:
                self.spoken.remove(row)
                break
        self.spoken.append((key, text, max(time.monotonic(), self.play_until)))

    def transcript_is_echo(self, text):
        if self.stop_request(text):
            return False
        words = self.normalized_words(text)
        if not words:
            return False
        now = time.monotonic()
        recent = []
        # Observed clipped/misheard copies of *our cached cues*, not a global
        # phrase blacklist. Single-word commands and arbitrary user questions
        # never match. Require speaker mode and matching playback or a 2s tail.
        cue_echoes = {"camera-taking": ("taking", "photo"),
                      "photo-ready": ("photo", "ready"),
                      "received": ("i", "hurt", "you")}
        for key, line, at in self.spoken:
            related = [c for c in self.clients if c.playback_id == key[0]]
            end = max([at] + [c.playback_end for c in related])
            playing = any(c.playback_active or c.playback_pending for c in related)
            kind = key[2] if len(key) > 2 and key[1] == "feedback" else None
            if (self.echo_guard and (playing or now - end <= 2)
                    and tuple(words) == cue_echoes.get(kind)):
                return True
            if playing or now - end <= 10:
                recent.append(line)
        # Echo = a long CONTIGUOUS run of the assistant's own words. Bag-of-words overlap
        # misfired on answers like "Yes, add them" to "...add them?" or "Cardinal." (10/23).
        minimum = 6 if self.chinese(text) else 3
        if len(words) < minimum:
            return False
        for line in recent + ([" ".join(recent)] if len(recent) > 1 else []):
            other = self.normalized_words(line)
            m = SequenceMatcher(None, words, other, autojunk=False).find_longest_match(0, len(words), 0, len(other))
            if m.size >= minimum and m.size >= .7 * len(words):
                return True
        return False

    def state_payload(self):
        return {"type": "state", "listening": self.mic_on,
                "speaking": time.monotonic() < self.play_until,
                "model": self.realtime.model, "provider": self.realtime.provider,
                "transport": self.transport,
                "features": {"audio_diagnostics": True, "webrtc": bool(getattr(app.state, "enable_webrtc", False))},
                "connected": self.connected, "echo_guard": self.echo_guard,
                "session": self.id, "server_build": SERVER_BUILD}

    def input_context(self):
        obs = self.observations.get(self.focus)
        return {"focus": self.focus, "image_revision": obs.image_revision if obs else None,
                "camera": self.camera_request["id"] if self.camera_request else None}

    def camera_route_current(self, name, args):
        """Validate ownership of a semantic camera action, never its wording."""
        if self.turn_revision is not None and self.turn_revision != self.input_revision:
            return False
        if self.turn_context is None:
            return True
        current = self.input_context()
        if self.turn_context["focus"] != current["focus"]:
            return False
        if self.turn_context["camera"] == current["camera"]:
            return True
        obs = self.observations.get(self.focus)
        # Completing the same capture is progress, not a new user operation.
        return (current["camera"] is None and obs is not None
                and obs.analysis_status == "ready"
                and obs.analysis_request_id == self.turn_context["camera"])

    def queue_feedback(self, kind, *, status=None):
        value = {"kind": kind, "revision": self.input_revision,
                 "language": "zh" if self.chinese(self.turn_question) else "en",
                 "focus": self.focus, "status": status}
        self.pending.append(("feedback", value, None))
        self.work_ready.set()

    def feedback_current(self, value):
        obs = self.observations.get(value["focus"])
        return (value["revision"] == self.input_revision and value["focus"] == self.focus
                and (not value["status"] or (obs and obs.analysis_status == value["status"])))

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
        focus = self.observations.get(self.focus)
        focus_context = ({"id": focus.id, "status": focus.status, "top3": focus.fused[:3],
                          "group": focus.group,
                          "analysis_status": focus.analysis_status,
                          "uncertain": focus.uncertain,
                          "image_detections": sum(d.kind == "image" for d in focus.detections)}
                         if focus and focus.status != "discarded" else None)
        actual_camera = {"phase": self.camera_request["phase"]} if self.camera_request else None
        answer_instructions = (ANSWER + "\nActual camera request state: " + json.dumps(actual_camera)
            + "\nOnly this current backend state establishes camera readiness. Never claim Camera ready or tell the user to say Start when it is null; an old photo's ready result is not a ready camera.")
        if self.expert_readout is not None:
            answer_instructions = ("Read the following naturalist answer aloud verbatim. "
                "Preserve its original language and every species identifier. "
                "Speak only this text, with no additions, hints, translation or explanation. "
                "Text to read: " + json.dumps(self.expert_readout, ensure_ascii=False))
        return self.realtime.phase({"output_modalities": ["text" if phase == "route" else "audio"],
                "instructions": (ROUTING + "\nFresh state snapshot (context only; sounds are unconfirmed): "
                                 + json.dumps(self.route_snapshot())
                                 + "\nCurrent pending BirdNET suggestions (unconfirmed; latest turn only): "
                                 + json.dumps([r["en"] for r in self.suggestions.values()])
                                 + "\nCurrent focus snapshot (context only, not a new sighting): "
                                 + json.dumps(focus_context)
                                 + "\nCamera confirmation state (start takes a photo ONLY after ready): "
                                 + json.dumps(self.camera_request)
                                 + "\nCurrent task status (authoritative stages, no findings; never restart to check progress): "
                                 + json.dumps(self.task_status_snapshot())
                                 + "\nActive task-selection question (a short reply selects one of these real IDs; otherwise null): "
                                 + json.dumps(self.task_status_context())
                                 + "\nLatest investigation explanation offer (accept only if it is the latest assistant question): "
                                 + json.dumps(self.investigation_offer_ids)
                                 + "\nInvestigations (context only; never invent progress): "
                                 + json.dumps([{**{k: row[k] for k in ("id", "species", "status", "stage")},
                                                "question": row["question"][:200]}
                                               for row in self.investigations.payload()["items"][-8:]])
                                 if phase == "route" else answer_instructions),
                "tools": TOOLS if phase == "route" else [],
                "tool_choice": "required" if phase == "route" else "none"})

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
        return self.realtime.session(self.config("route"))

    def realtime_key(self):
        return self.realtime.openai_key() if self.realtime.provider == "openai" else api_key()

    async def update_session(self, config):
        # Reader resolves the echo future; no waiting inside the reader.
        async with self.update_lock:
            self.update_waiter = asyncio.get_running_loop().create_future()
            self.update_expected = config
            try:
                await self.rt_send({"type": "session.update", "session": config})
                echo = await asyncio.wait_for(self.update_waiter, 12)
                for key in ("tool_choice", "output_modalities", "instructions"):
                    if key in config and echo.get(key) != config[key]:
                        raise RuntimeError("Session echo mismatch: " + key)
                self.log("session_updated", phase=self.phase)
            finally:
                self.update_waiter = None
                self.update_expected = None

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
                owner = self.item_owners.pop(item_id, None)
                if owner is not None:
                    owner.discard(item_id)
                self.deleted_item_ids.append(item_id)

    async def generate(self, phase, *, guard=None):
        token = {"cancelled": False, "requested": False, "response_id": None,
                 "cancel": asyncio.get_running_loop().create_future()}
        self.generation = token
        try:
            return await self.generate_response(phase, token, guard=guard)
        finally:
            if self.generation is token:
                self.generation = None

    async def wait_cancel_boundary(self, token):
        barrier = self.cancel_barrier
        if barrier is None:
            return
        waiters = [barrier["future"]] + ([token["cancel"]] if "cancel" in token else [])
        await asyncio.wait(waiters, timeout=1, return_when=asyncio.FIRST_COMPLETED)
        if token["cancelled"]:
            return
        acknowledged = barrier["future"].done()
        if (not acknowledged or barrier["unknown"]) and self.rt is barrier["rt"] and hasattr(barrier["rt"], "close"):
            # No response ID means a late response cannot be correlated safely.
            # Recover the model connection before starting another generation;
            # local camera/PCM controls continue on the phone socket.
            old = barrier["rt"]
            self.log("cancel_transport_recovery", unknown_response=barrier["unknown"])
            try:
                await asyncio.wait_for(old.close(), 2)
            except asyncio.TimeoutError:
                if getattr(old, "transport", None) is not None:
                    old.transport.abort()
            async with asyncio.timeout(10):
                while (self.rt is old or not self.connected) and not token["cancelled"]:
                    await asyncio.sleep(.02)
        if self.cancel_barrier is barrier:
            self.cancel_barrier = None

    async def generate_response(self, phase, token, *, guard=None):
        await self.wait_cancel_boundary(token)
        if token["cancelled"]:
            return {"status": "cancelled", "output": []}
        self.phase = phase
        updating = asyncio.create_task(self.update_session(self.config(phase)))
        try:
            await asyncio.wait((updating, token["cancel"]), return_when=asyncio.FIRST_COMPLETED)
            if token["cancelled"]:
                return {"status": "cancelled", "output": []}
            await updating
        finally:
            if not updating.done():
                updating.cancel()
            await asyncio.gather(updating, return_exceptions=True)
        if phase == "answer" and self.auto_speech_paused:
            return {"status": "deferred"}  # A stop during setup must not restart speech.
        if guard is not None and not guard():
            return {"status": "deferred"}
        self.response_id = None
        self.audio_allowed = phase == "answer" and self.may_speak()
        self.first_audio = False
        for attempt in range(2):
            prior_items = set(self.turn_items or ())
            self.response_id = None
            token["response_id"] = None
            self.response_waiter = asyncio.get_running_loop().create_future()
            self.awaited_response_id = None
            try:
                if token["cancelled"]:
                    return {"status": "cancelled", "output": []}
                token["requested"] = True
                await self.rt_send({"type": "response.create"})
                try:
                    response = await asyncio.wait_for(asyncio.shield(self.response_waiter), 8 if phase == "route" else 25)
                except asyncio.TimeoutError:
                    self.log("response_timeout", phase=phase)
                    if self.awaited_response_id:
                        self.cancelled_ids.append(self.awaited_response_id)
                    self.audio_allowed = False
                    await self.rt_send({"type": "response.cancel"})
                    await self.end_audio()
                    if phase != "route" or attempt or self.tools_executed:
                        raise
                    await self.delete_items(set(self.turn_items or ()) - prior_items)
                    continue
                if token["cancelled"]:
                    response = {"status": "cancelled", "output": []}
                for item in response.get("output", []):
                    self.own_item(item.get("id"))
                if response.get("status") == "cancelled":
                    await self.delete_items(set(self.turn_items or ()) - prior_items)
                if response.get("status") == "failed":
                    if phase == "route" and not attempt and not self.tools_executed:
                        if token["response_id"]:
                            self.cancelled_ids.append(token["response_id"])
                        self.log("route_retry", reason="failed")
                        # A partial failed function call must not poison the retry.
                        await self.delete_items(set(self.turn_items or ()) - prior_items)
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
            else:
                progress = next((p for p in self.pending if p[0] == "feedback"), None)
                progress = progress or next((p for p in self.pending if p[0] == "camera_start"), None)
                progress = progress or next((p for p in self.pending if p[0] == "camera_photo"), None)
                vision = progress or next((p for p in self.pending if p[0] == "vision"), None)
                if vision is not None:
                    self.pending.remove(vision)
                    self.pending.appendleft(vision)
            kind, value, stopped = self.pending[0]
            if kind == "feedback" and not self.feedback_current(value):
                self.pending.popleft()
                self.work_ready.set()
                continue
            if kind == "text" and stopped is not None:
                # Merge consecutive queued utterances into one turn (backlog after a slow turn).
                parts = [value]
                acknowledged = getattr(value, "acknowledged", False)
                merged_ids = set(getattr(value, "item_ids", ()))
                while len(self.pending) > 1 and self.pending[1][0] == "text" and self.pending[1][2] is not None:
                    _, more, stopped = self.pending[1]
                    del self.pending[1]
                    parts.append(more)
                    acknowledged = acknowledged or getattr(more, "acknowledged", False)
                    merged_ids.update(getattr(more, "item_ids", ()))
                if len(parts) > 1:
                    value = TurnText(" ".join(parts), merged_ids, acknowledged, getattr(parts[-1], "context", None))
                    self.pending[0] = (kind, value, stopped)
                    self.log("turns_merged", n=len(parts))
            self.turn_stale = stopped is not None and time.monotonic() - stopped > 12
            if kind in ("notify", "investigation", "camera_ready", "camera_photo", "vision", "feedback", "task_status") and self.auto_speech_paused:
                continue
            start_acknowledged = (kind == "camera_start" and self.camera_request
                                  and self.camera_request.get("start_acknowledged"))
            if kind in ("notify", "investigation", "camera_ready", "camera_start", "camera_photo", "vision", "feedback", "task_status") and self.playback_pending() and not start_acknowledged:
                continue
            if kind in ("camera_start", "camera_photo", "vision", "feedback", "task_status") and (self.awaiting_transcript
                    or (time.monotonic() < self.play_until and not start_acknowledged)):
                continue
            if kind == "camera_ready" and (self.awaiting_transcript or not self.may_speak()
                    or time.monotonic() < self.play_until):
                continue
            if kind == "notify" and (self.awaiting_transcript or self.quiet
                                     or time.monotonic() < self.play_until
                                     or time.monotonic() - self.last_proactive < SUGGEST_INTERVAL):
                continue
            if kind == "investigation" and (self.awaiting_transcript or self.quiet
                    or time.monotonic() < self.play_until
                    or self.echo_blocked() or (stopped is not None and time.monotonic() < stopped)):
                continue
            self.pending.popleft()
            if kind == "notify":
                batch = self.suggestion_batch()
                if not batch:
                    self.work_ready.set()
                    continue
            self.active = True
            self.turn_in_progress = True
            self.turn_stopped_at = stopped if kind in ("text", "audio") else None
            self.turn_items = TurnItems(getattr(value, "item_ids", ()))
            self.input_acknowledged = getattr(value, "acknowledged", False)
            self.turn_context = getattr(value, "context", None)
            self.turn_revision = self.input_revision
            for item_id in self.turn_items:
                self.item_owners[item_id] = self.turn_items
            self.tools_executed = False
            try:
                if kind == "feedback":
                    await self.play_feedback(value["kind"], language=value["language"],
                        guard=lambda: self.feedback_current(value))
                elif kind == "camera_ready":
                    request = self.camera_request
                    if request and request["id"] == value and request["phase"] == "ready":
                        played = await self.play_feedback("camera-ready", language=request["language"],
                            guard=lambda: self.camera_request is request and request["phase"] == "ready")
                        if not played and self.camera_request is request and request["phase"] == "ready":
                            self.pending.append(("camera_ready", value, None))
                elif kind == "camera_start":
                    await self.start_camera_capture(value)
                elif kind == "camera_photo":
                    await self.announce_camera_photo(value)
                elif kind == "investigation":
                    job = self.investigations.jobs.get(value)
                    if job is not None:
                        await self.investigation_notice(job)
                elif kind == "vision":
                    await self.answer_vision(value)
                elif kind == "task_status":
                    if value["revision"] == self.input_revision:
                        self.turn_question = value["question"]
                        await self.speak_task_status(value["args"])
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
                        self.turn_question = value
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
                if kind != "investigation":
                    await self.error("Turn failed; please try again.")
            finally:
                self.turn_items = None
                self.turn_question = ""
                self.input_acknowledged = False
                self.turn_context = None
                self.turn_revision = None
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
        words = " ".join(self.normalized_words(str(self.turn_question)))
        idle_start = (not self.camera_request and words in ("start", "start please", "开 始", "开 始 吧"))
        if idle_start:
            self.log("camera_guard", requested_tool=name, reason="no_pending_capture")
            self.queue_feedback("clarify")
            call = {**call, "name": "ignore", "arguments": "{}"}
            name = "ignore"
        try:
            args = json.loads(call.get("arguments") or "{}")
            if not isinstance(args, dict):
                raise ValueError("Tool arguments must be an object")
            if name not in {t["name"] for t in TOOLS}:
                raise ValueError("Unknown routed tool")
            if "group" in args and args["group"] not in GROUPS:
                raise ValueError("invalid iconic group")
            if name == "look" and args.get("target") not in ("current", "new"):
                raise ValueError("look requires target=current or target=new")
            if name == "look" and "hint" in args and not isinstance(args["hint"], str):
                raise ValueError("look hint must be text")
            if name == "describe" and not isinstance(args.get("text"), str):
                raise ValueError("describe requires text")
            if name == "recent_sounds" and not np.isfinite(float(args.get("seconds", 15))):
                raise ValueError("seconds must be finite")
            decision, reason = route_guard(name, args, self.turn_question,
                suggestions=bool(self.suggestions), **self.consent_context())
            if ((name in WRITE_TOOLS or name == "discard_observation") and self.turn_context is not None
                    and self.turn_context != self.input_context()):
                decision, reason = "clarify", "stale_write_context"
            if name in ("look", "new_observation") and not self.camera_route_current(name, args):
                decision, reason = "ignore", "stale_camera_context"
            if decision != "allow":
                self.log("route_guard", requested_tool=name, decision=decision, reason=reason)
                result = {"ok": False, "suppress_answer": True, "guard": reason}
                if decision == "clarify":
                    self.queue_feedback("clarify")
                await self.rt_send({"type": "conversation.item.create", "item": {
                    "type": "function_call_output", "call_id": call["call_id"],
                    "output": json.dumps(result)}})
                return
            await self.acknowledge_tool(name)
            # Local feedback yields to microphone/UI events. Recheck ownership
            # before a delayed route can replace a newer camera request.
            if name in ("look", "new_observation") and not self.camera_route_current(name, args):
                await self.rt_send({"type": "conversation.item.create", "item": {
                    "type": "function_call_output", "call_id": call["call_id"],
                    "output": json.dumps({"ok": False, "suppress_answer": True, "guard": "stale_camera_context"})}})
                self.log("route_guard", requested_tool=name, decision="ignore", reason="stale_camera_context")
                return
            self.tools_executed = True
            result = {"ok": True} if name == "ignore" else (
                {"ready_to_answer": True, "reason": args.get("reason")} if name == "speak"
                else await self.tool(name, args))
            if name == "get_observation" and result.get("analysis_status") in ("awaiting_confirmation", "capturing", "analyzing", "failed"):
                status = result["analysis_status"]
                ready = self.camera_request and self.camera_request["phase"] == "ready"
                kind = ("camera-ready" if ready else "camera-opening") if status == "awaiting_confirmation" else (
                    "camera-taking" if status == "capturing" else "photo-failed" if status == "failed" else "photo-processing")
                self.queue_feedback(kind, status=status)
                result["suppress_answer"] = True
            if result.get("analysis_pending"):
                status = result.get("analysis_status", "analyzing")
                self.queue_feedback("camera-taking" if status == "capturing" else "photo-processing")
                result["suppress_answer"] = True
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
            elif not result.get("analysis_pending"):
                await self.error(result.get("error", "Checklist change failed"))
        await self.rt_send({"type": "conversation.item.create", "item": {
            "type": "function_call_output", "call_id": call["call_id"],
            "output": json.dumps(result, ensure_ascii=False)}})
        self.log("tool", name=name, arguments=self.audit_args(call.get("arguments")), ok=result.get("ok", True))
        if result.get("suppress_answer"):
            return
        if name in ("look", "new_observation") and not result.get("ok"):
            self.queue_feedback("clarify")
            return
        if name == "get_task_status" and not result.get("ok"):
            self.queue_feedback("clarify")
            return
        if name in ("speak", "get_checklist", "recent_sounds", "nearby_species", "get_observation", "ask_expert", "describe", "get_investigations", "manage_investigation", "get_task_status") and self.may_speak():
            if (getattr(self, "turn_stale", False) or (self.turn_stopped_at is not None
                    and time.monotonic() - self.turn_stopped_at > 12)):
                self.log("stale_answer_skipped")  # answering a question asked >12 s ago is confusing
                return
            if name == "get_task_status":
                await self.speak_task_status(args)
            elif name in ("ask_expert", "get_investigations", "manage_investigation") and result.get("answer"):
                await self.speak_expert(result["answer"])
            else:
                read_identity = (result.get("observation", {}).get("id"),
                                 result.get("observation", {}).get("image_revision"))
                response = await self.generate("answer")
                if name == "get_observation" and response and response.get("status") == "completed":
                    if ((obs := self.observations.get(self.focus)) and obs.analysis_status == "ready"
                            and (obs.id, obs.image_revision) == read_identity):
                        self.vision_answered.add((obs.id, obs.image_revision))

    def latency(self, target):
        if self.turn_stopped_at is not None:
            self.log("latency", target=target,
                     speech_stopped_ms=round((time.monotonic() - self.turn_stopped_at) * 1000, 1))

    async def realtime_loop(self):
        delay = 1
        while not self.closed:
            try:
                key = self.realtime_key()
                async with websockets.connect(self.realtime.url,
                        additional_headers={"Authorization": "Bearer " + key}, max_size=None,
                        open_timeout=20, ping_interval=20, ping_timeout=20) as ws:
                    self.rt = ws
                    self.connected = False
                    self.active = False
                    self.response_id = None
                    self.awaited_response_id = None
                    self.user_speaking = self.awaiting_transcript = False
                    self.input_audio_turns.clear()
                    self.current_input_audio_turn = None
                    self.conversation_ids.clear()
                    self.deleted_item_ids.clear()
                    self.item_owners.clear()
                    self.response_owners.clear()
                    self.transcripts.clear()
                    initial_config = self.session()
                    await self.rt_send({"type": "session.update", "session": initial_config})
                    # OpenAI first emits a default session.created. Do not start
                    # mic ingestion/routing until our actual config is acknowledged.
                    async with asyncio.timeout(20):
                        while True:
                            event = json.loads(await ws.recv())
                            if event.get("type") == "error":
                                await self.handle_rt(event)
                                raise RuntimeError("Realtime setup rejected")
                            if self.realtime.accepts_initial(event, initial_config):
                                break
                    self.connected = True
                    delay = 1
                    self.log("realtime_connected", provider=self.realtime.provider,
                             model=self.realtime.model, acknowledgement=event.get("type"))
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
                self.input_audio_turns.clear()
                self.current_input_audio_turn = None
                self.active = False
                if not self.closed:
                    self.play_until = 0
                    await self.flush_audio()
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
            if rid in self.cancelled_ids:
                if item_id:
                    await self.delete_items([item_id])
                return
            owner = self.response_owners.get(rid)
            self.own_item(item_id, owner)
            if owner is not None and getattr(owner, "failed", False) and item_id:
                await self.delete_items([item_id])
            return
        if ty == "session.updated":
            if self.update_waiter is not None and not self.update_waiter.done():
                echo = event.get("session", {})
                if self.update_expected and any(echo.get(k) != self.update_expected[k]
                        for k in ("tool_choice", "output_modalities", "instructions") if k in self.update_expected):
                    self.log("stale_session_update_ignored")
                else:
                    self.update_waiter.set_result(echo)
            return
        if ty in output_types and (self.phase != "answer" or not self.audio_allowed):
            return
        if ty in output_types and event.get("response_id") is not None and rid != self.response_id:
            return  # A late chunk from another response must never join this voice turn.
        if ty == "error":
            code = (event.get("error") or {}).get("code", "unknown")
            message = str((event.get("error") or {}).get("message", ""))[:500]
            try:
                message = message.replace(self.realtime_key(), "[redacted]")
            except RuntimeError:
                pass
            self.log("realtime_error", code=code, message=message)
            inactive_cancel = code in ("response_not_active", "response_cancel_not_active")
            if inactive_cancel and self.cancel_barrier is not None:
                future = self.cancel_barrier["future"]
                if not future.done():
                    future.set_result(None)
            if not inactive_cancel and "cancel" not in str(code):
                for waiter in (self.update_waiter, self.response_waiter):
                    if waiter is not None and not waiter.done():
                        waiter.set_exception(RuntimeError("Realtime request rejected"))
                await self.error("Realtime request failed (" + str(code)[:100] + ").")
        elif ty == "input_audio_buffer.speech_started":
            self.log("input_vad_start", item_id=event.get("item_id"))
            record = {"id": event.get("item_id"), "blocked": self.echo_blocked(),
                      "at": time.monotonic(), "stopped": None}
            self.input_audio_turns.append(record)
            self.prune_input_audio_turns()
            if record["blocked"]:
                self.log("input_audio_guarded", event_type=ty, item_id=record["id"], reason="playback")
                return
            self.current_input_audio_turn = record
            self.input_revision += 1
            self.user_speaking = True
            self.awaiting_transcript = True
            if self.assistant_audio_pending():
                await self.voice_started(record)
            await self.state()
        elif ty == "input_audio_buffer.speech_stopped":
            self.log("input_vad_stop", item_id=event.get("item_id"))
            record = self.match_input_audio_turn(event)
            if record is not None:
                record["stopped"] = time.monotonic()
            if (record is not None and record["blocked"]) or self.echo_blocked():
                return
            if record is self.current_input_audio_turn:
                self.user_speaking = False
                self.speech_stopped_at = time.monotonic()
        elif ty == "conversation.item.input_audio_transcription.completed":
            if not await self.finish_input_audio(event):
                return
            text = event.get("transcript", "")
            if self.transcript_is_echo(text):
                self.log("echo_dropped", text=text)
                if event.get("item_id"):
                    await self.delete_items([event["item_id"]])
                await self.flush_pending()
                return
            ignored = self.ignored_input_reason(text)
            if ignored:
                self.log("input_ignored", text=text, reason=ignored)
                if event.get("item_id"):
                    await self.delete_items([event["item_id"]])
                await self.flush_pending()
                return
            if text.strip() and self.assistant_audio_pending():
                await self.cancel_speech()
                self.log("barge_in", confirmation="transcript")
            await self.phone_json({"type": "transcript", "role": "user", "text": text, "final": True})
            self.log("user_transcript", text=text)
            if text.strip():
                self.input_revision += 1
                if self.stop_request(text):
                    self.auto_speech_paused = True
                    await self.flush_pending()
                    return  # Stay listening; a stop command needs no spoken reply.
                self.auto_speech_paused = False
                if await self.camera_confirmation(text):
                    await self.flush_pending()
                    return
                self.pending.append(("text", TurnText(text, [event["item_id"]] if event.get("item_id") else [],
                                                       context=self.input_context()), self.speech_stopped_at))
                await self.flush_pending()
        elif ty == "conversation.item.input_audio_transcription.failed":
            if not await self.finish_input_audio(event):
                return
            await self.error("Input transcription failed.")
            await self.flush_pending()
        elif ty == "response.created":
            response_id = event["response"]["id"]
            if response_id in self.cancelled_ids or (self.generation and self.generation["cancelled"]):
                self.cancelled_ids.append(response_id)
                self.log("late_response_ignored", response_id=response_id)
                return
            if self.generation is not None:
                self.generation["response_id"] = response_id
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
                await self.begin_audio(rid)
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
                self.log("assistant_transcript", response_id=rid,
                         text=event.get("transcript", event.get("text", ""))[:1200])
                self.last_assistant_text = event.get("transcript", event.get("text", ""))
        elif ty == "response.done":
            response = event["response"]
            if self.cancel_barrier is not None and response.get("id") == self.cancel_barrier.get("response_id"):
                future = self.cancel_barrier["future"]
                if not future.done():
                    future.set_result(None)
            if response.get("id") in self.cancelled_ids:
                return  # A late completion must not finish newer cached audio.
            expected = self.awaited_response_id or self.response_id
            if expected is not None and response.get("id") != expected:
                return
            self.log("response_done", status=response.get("status"), phase=self.phase)
            if self.phase == "answer" and self.first_audio:
                await self.end_audio()
            if (self.response_waiter is not None and not self.response_waiter.done()
                    and response.get("id") == expected):
                # A matching cancelled response releases the worker for the next
                # question. Cancelled PCM remains blocked by cancelled_ids.
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
        if name == "get_task_status":
            return await self.task_status(args)
        if name in ("get_investigations", "manage_investigation"):
            return await self.investigation_tool(name, args)
        if name == "look":
            target = args.get("target", "current")
            if target not in ("current", "new"):
                raise ValueError("invalid camera target")
            if target == "new":
                group = args.get("group", "Aves")
                if group not in GROUPS:
                    raise ValueError("invalid iconic group")
                await self.new_focus(group)
                return await self.request_look(args.get("hint", ""))
            return await self.request_look(args.get("hint", ""), args.get("group"))
        if name == "new_observation":
            group = args.get("group", "Aves")
            if group not in GROUPS:
                raise ValueError("invalid iconic group")
            await self.new_focus(group)
            return await self.request_look()
        if name in ("get_observation", "confirm_observation", "discard_observation", "ask_expert", "describe"):
            obs = await self.current_focus(create=name == "describe")
            if not obs:
                return {"ok": False, "error": "No focus observation; look first"}
            if name == "get_observation":
                return {"observation": obs.payload(), "analysis_status": obs.analysis_status}
            if name == "ask_expert":
                # The router can paraphrase Chinese into English or add claims.
                # Use this turn's actual text/STT transcript when available.
                return await self.ask_expert(self.turn_question or args["question"], obs)
            if name in ("confirm_observation", "discard_observation"):
                if name == "confirm_observation" and obs.analysis_status in ("awaiting_confirmation", "capturing", "analyzing"):
                    return {"ok": False, "analysis_pending": True, "error": "Wait for the current photo before confirming"}
                await self.cancel_camera_request()
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
                if obs and obs.group == "Aves" and obs.status == "open" and abs(stamp - obs.touched) <= 10:
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
        contaminated = self.environment_contaminated()
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
        if self.transport == "websocket" and self.connected and not self.echo_blocked():
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
                async with self.rt_lock:
                    # Playback can begin while a queued packet waits for this
                    # lock. Recheck at transmission, not only at capture time.
                    if self.connected and self.rt is not None and not self.echo_blocked():
                        await self.rt.send(json.dumps({"type": "input_audio_buffer.append",
                            "audio": base64.b64encode(pcm).decode()}))
            except Exception as exc:
                self.log("audio_send_failed", error_type=type(exc).__name__)
            finally:
                self.audio_queue.task_done()

    async def handle_phone(self, event, client=None):
        client = client or self.mic or next((c for c in self.clients if c.role == "mic"), None)
        ty = event.get("type")
        if ty == "diagnostics":
            if client is not self.mic or not isinstance(event.get("enabled"), bool):
                raise ValueError("Only the current phone controls diagnostics")
            client.diagnostics = event["enabled"]
            self.log("diagnostics", enabled=client.diagnostics, transport=self.transport)
            return
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
            if client.playback_protocol == 1:
                if not isinstance(event.get("drained"), bool):
                    raise ValueError("playback.drained must be boolean")
                if event.get("response_id") != client.playback_id:
                    return  # An old playback completion cannot release a newer reply.
                if event["drained"] and event["active"]:
                    raise ValueError("Active playback cannot be drained")
                client.playback_pending = not event["drained"]
            client.playback_active = event["active"]
            if not client.playback_active and not client.playback_pending:
                client.playback_end = time.monotonic()
            self.log("playback", active=client.playback_active, role=client.role)
            await self.state()
            await self.flush_pending()
            return
        if ty == "reset":
            await self.reset()
            return
        if ty in ("camera_ready", "camera_closed"):
            if client is None or client is not self.mic:
                raise ValueError("Only the current microphone controls its camera")
            if ty == "camera_ready":
                if not event.get("request_id"):
                    self.input_revision += 1  # A manually opened/changed preview.
                await self.camera_ready(event)
            elif self.camera_request and (event.get("request_id") == self.camera_request["id"]
                    or (event.get("open_id") and event["open_id"] == self.camera_request.get("open_id"))):
                self.input_revision += 1
                await self.cancel_camera_request()
            return
        if ty in ("hello", "location"):
            if ty == "location" and (event.get("lat") is None or event.get("lng") is None):
                raise ValueError("location requires lat and lng")
            if ty == "hello" and client is not None:
                client.playback_protocol = 1 if event.get("playback_protocol") == 1 else 0
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
            await self.phone_json(self.investigations.payload())
        elif ty == "investigation_action":
            result = await self.investigation_tool("manage_investigation", {
                "id": event["id"], "action": event["action"]})
            if not result.get("ok"):
                raise ValueError(result.get("answer", "Investigation action failed"))
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
                await self.flush_audio()
            await self.state()
            await self.flush_pending()
        elif ty == "output_mute":
            if not isinstance(event.get("muted"), bool):
                raise ValueError("muted must be boolean")
            # Diagnostic only: do not alter microphone/BirdNET or playback gates.
            self.log("output_mute", muted=event["muted"], role=client.role if client else None)
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
            if self.stop_request(text):
                self.auto_speech_paused = True
                await self.cancel_speech()
                await self.flush_pending()
                return
            self.auto_speech_paused = False
            if await self.camera_confirmation(text):
                await self.flush_pending()
                return
            if self.assistant_audio_pending():
                await self.cancel_speech()
            self.pending.append(("text", TurnText(text, context=self.input_context()), None))
            await self.flush_pending()
        elif ty == "look":
            self.input_revision += 1
            self.auto_speech_paused = False
            if not isinstance(event.get("confirmed", False), bool):
                raise ValueError("confirmed must be boolean")
            if event.get("confirmed") and self.assistant_audio_pending():
                await self.cancel_speech()
            await self.request_look(event.get("hint", ""), event.get("group"), confirmed=event.get("confirmed", False))
        elif ty == "observation_action":
            obs = self.observations.get(event.get("id"))
            if not obs or obs.status == "discarded":
                raise ValueError("Unknown or discarded observation")
            action = event.get("action")
            if action not in ("focus", "confirm", "discard"):
                raise ValueError("Unknown observation action")
            old = self.observations.get(self.focus)
            await self.cancel_camera_request()
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
        self.vision_answered.clear()
        self.last_assistant_text = ""
        await self.cancel_camera_request()
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
        await self.phone_json(self.investigations.payload())

    async def reset(self):
        self.epoch += 1
        if self.turn_task is not None:
            self.turn_task.cancel()
            await asyncio.gather(self.turn_task, return_exceptions=True)
            self.tasks.remove(self.turn_task)
            self.turn_task = None
        await self.investigations.close()
        self.investigations = InvestigationQueue(self.run_investigation,
            lambda event: self.phone_json(event), self.investigation_finished,
            settings=self.investigations.settings)
        self.investigation_offer_ids.clear()
        self.task_status_offer = None
        self.auto_speech_paused = False
        self.expert_readout = None
        self.turn_question = ""
        self.turn_in_progress = False
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
        self.input_audio_turns.clear()
        self.current_input_audio_turn = None
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
        token = self.generation
        send_cancel = self.connected and self.active and self.cancel_barrier is None
        if token is not None:
            send_cancel = self.connected and token["requested"] and not token["cancelled"]
            token["cancelled"] = True
            if not token["cancel"].done():
                token["cancel"].set_result(None)
            if self.response_waiter is not None and not self.response_waiter.done():
                self.response_waiter.set_result({"status": "cancelled", "output": []})
            if send_cancel:
                self.cancel_barrier = {"future": asyncio.get_running_loop().create_future(),
                    "unknown": token["response_id"] is None, "rt": self.rt,
                    "response_id": token["response_id"]}
            self.log("generation_cancelled", response_id=token["response_id"], requested=token["requested"])
        if send_cancel:
            await self.rt_send({"type": "response.cancel"})
        await self.flush_audio()
        await self.state()

    def start(self):
        self.turn_task = asyncio.create_task(self.turn_worker())
        self.tasks = [asyncio.create_task(self.realtime_loop()),
                      asyncio.create_task(self.audio_sender()), self.turn_task,
                      asyncio.create_task(self.focus_watch()),
                      asyncio.create_task(self.suggestion_loop())]

    async def close(self):
        self.closed = True
        await self.investigations.close()
        tasks = self.tasks + list(self.diagnostic_tasks) + [t for t in (self.bird_task, self.playback_task) if t is not None]
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
        self.playback_protocol = 0
        self.playback_pending = False
        self.playback_id = None
        self.playback_end = 0.0
        self.diagnostics = False

    async def send_json(self, event):
        async with self.lock:
            await self.phone.send_json(event)

    async def send_audio(self, pcm, *, guard=None):
        async with self.lock:
            if guard is None or guard():
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
            await s.cancel_camera_request()
            s.mic = self
            if old is not None:
                s.clients.discard(old)
                try:
                    await old.phone.close(code=1000, reason="Replaced by newer microphone")
                except (RuntimeError, WebSocketDisconnect):
                    pass
        try:
            if self.role == "mic" and s.mic is self:
                await s.mic_attached(self)
            await s.state()
            await self.send_json({"type": "checklist", "items": [dict(r) for r in s.items]})
            for obs in list(s.observations.values()):
                await self.send_json({"type": "observation", **obs.payload(), "focus": s.focus == obs.id})
            await self.send_json(s.suggestions_payload())
            await self.send_json(s.investigations.payload())
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
                        if self.role == "display" and event.get("type") not in ("hello", "text", "edit", "delete", "tap", "suggestion_action", "playback", "look", "observation_action", "investigation_action", "reset"):
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
                await s.cancel_camera_request()
                s.mic = None
                await s.mic_detached(self)
            await s.state()
            if not s.clients:
                s.expiry_task = asyncio.create_task(s.expire())


app = FastAPI(title="Field Companion v0.2")


@app.websocket("/ws")
async def phone_socket(ws: WebSocket):
    role = ws.query_params.get("role", "mic")
    session_id = ws.query_params.get("session", "demo")
    transport = ws.query_params.get("transport", "websocket")
    if (role not in ("mic", "display") or not session_id or len(session_id) > 128
            or transport not in ("websocket", "webrtc")):
        await ws.close(code=1008)
        return
    if transport == "webrtc" and (not getattr(app.state, "enable_webrtc", False)
            or (role == "display" and ws.query_params.get("audio") == "1")):
        await ws.close(code=1008, reason="WebRTC experiment unavailable or display audio unsupported")
        return
    session = SESSIONS.get(session_id)
    if session is not None and session.transport != transport:
        await ws.close(code=1008, reason="Use a separate session for each audio transport")
        return
    await ws.accept()
    if session is None:
        if transport == "webrtc":
            from realtime_webrtc import make_session
            session = make_session(Session, session_id)
        else:
            session = Session(session_id)
        SESSIONS[session_id] = session
        session.start()
    await Client(ws, session, role, ws.query_params.get("audio") == "1").run()


@app.post("/api/realtime/webrtc")
async def webrtc_offer(request: Request, session: str):
    current = SESSIONS.get(session)
    if not getattr(app.state, "enable_webrtc", False) or current is None or current.transport != "webrtc":
        raise HTTPException(404, "WebRTC experiment is not enabled")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 65536:
            raise HTTPException(413, "SDP offer too large")
    try:
        sdp = body.decode("utf-8")
        if not sdp.startswith("v=0") or "m=audio" not in sdp or "a=fingerprint:" not in sdp:
            raise ValueError("Invalid audio offer")
        answer = await current.offer(request.headers.get("x-field-rtc", ""), sdp)
        return Response(answer, media_type="application/sdp", headers={"Cache-Control": "no-store"})
    except PermissionError:
        raise HTTPException(403, "Reconnect the phone before starting WebRTC") from None
    except (ValueError, UnicodeError):
        raise HTTPException(400, "Invalid or duplicate audio offer") from None
    except Exception as exc:
        current.log("webrtc_offer_failed", error_type=type(exc).__name__)
        raise HTTPException(502, "Voice setup failed; end the session and try again") from None


@app.post("/api/frame")
async def post_frame(request: Request, session: str, obs: str, x: float | None = None,
                     y: float | None = None, w: float | None = None, h: float | None = None,
                     capture: str | None = None):
    s = SESSIONS.get(session)
    if not s:
        raise HTTPException(404, "Unknown session; connect WebSocket first")
    await s.expire_focus()
    observation = s.observations.get(obs)
    if not observation or observation.status != "open" or s.focus != obs:
        raise HTTPException(409, "Frame requires the current open focus observation")
    camera = s.camera_request
    if (not camera or capture != camera["id"] or camera["obs"] != obs
            or not camera["confirmed"] or camera["phase"] != "capturing"):
        raise HTTPException(409, "Confirm the ready camera before taking a photo")
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
    # Reading the body yields: another upload or a close may have consumed the
    # confirmation in the meantime. Claim it exactly once before any inference.
    if s.camera_request is not camera or camera["phase"] != "capturing":
        raise HTTPException(409, "Capture cancelled or already submitted")
    camera["phase"] = "classifying"
    camera["uploaded_at"] = time.monotonic()
    observation.analysis_status = "analyzing"
    observation.analysis_stage = "range_lookup" if observation.group == "Aves" else "recognizing"
    await s.broadcast_observation(observation)
    if s.camera_request is not camera:
        raise HTTPException(409, "Capture cancelled before classification")
    s.camera_feedback_request = camera
    s.pending.append(("camera_photo", camera["id"], None))
    await s.flush_pending()
    s.log("camera_photo_received", request_id=camera["id"])
    try:
        allowed = await asyncio.get_running_loop().run_in_executor(s.executor, s.allowed_species) if observation.group == "Aves" else None
        if not await s.photo_stage(observation, camera, "recognizing"):
            raise HTTPException(409, "Capture cancelled before image recognition")
        result = await asyncio.to_thread(classify_frame, jpeg, crop, sorted(allowed) if allowed else None)
    except Exception:
        await s.photo_failed(observation, camera)
        raise
    if not await s.photo_stage(observation, camera, "resolving_species"):
        raise HTTPException(409, "Capture cancelled before species lookup")
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
    if observation.status != "open" or s.focus != obs or s.camera_request is not camera:
        raise HTTPException(409, "Observation closed or focus changed during classification")
    detection_id = "d" + uuid.uuid4().hex[:12]
    detection = Detection(detection_id, "image", now_iso(), candidates, thumb="/api/frame/" + detection_id + ".jpg")
    s.frames[detection_id] = thumb.getvalue()
    observation.detections.append(detection)
    observation.image_revision += 1
    if not await s.photo_stage(observation, camera, "fusing"):
        raise HTTPException(409, "Capture cancelled before evidence fusion")
    try:
        await s.refusion(observation)
    except Exception:
        await s.photo_failed(observation, camera)
        raise
    if s.camera_request is not camera:
        raise HTTPException(409, "Capture cancelled while updating evidence")
    s.camera_request = None
    observation.analysis_status = "ready"
    observation.analysis_stage = "ready"
    observation.analysis_finished = time.monotonic()
    s.log("classification_completed", request_id=camera["id"], observation_id=obs,
          image_revision=observation.image_revision, uncertain=observation.uncertain,
          duration_ms=round((time.monotonic() - camera["uploaded_at"]) * 1000),
          raw_top=result["candidates"][:2], fused_top=observation.fused[:2])
    if observation.awaiting_answer:
        observation.awaiting_answer = False
        s.pending.append(("vision", {"obs": obs, "image_revision": observation.image_revision,
                                     "request_id": camera["id"]}, None))
    # Publish the result only after committing flags/queue atomically. A new
    # Start arriving during the broadcast must retain its own awaiting flag.
    await s.broadcast_observation(observation)
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
    parser.add_argument("--realtime-provider", choices=("higgs", "openai"), default=None,
                        help="Realtime conversation provider (default: existing Higgs configuration)")
    parser.add_argument("--realtime-model", default=None, help="Override the OpenAI Realtime model")
    parser.add_argument("--enable-webrtc", action="store_true", help="Enable the opt-in OpenAI browser media experiment")
    args = parser.parse_args()
    if args.realtime_provider:
        os.environ["REALTIME_PROVIDER"] = args.realtime_provider
    if args.realtime_model:
        if RealtimeConfig.load().provider != "openai":
            parser.error("--realtime-model requires --realtime-provider openai")
        os.environ["OPENAI_REALTIME_MODEL"] = args.realtime_model
    selected_realtime = RealtimeConfig.load()
    app.state.enable_webrtc = args.enable_webrtc
    if args.enable_webrtc:
        from realtime_webrtc import openai_config
        openai_config().openai_key()
    if selected_realtime.provider == "openai":
        selected_realtime.openai_key()  # Fail before accepting sessions if unconfigured.
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(asctime)s %(levelname)s %(message)s")
    # Never enable websocket HTTP/header debug logging: it can expose Authorization.
    logging.getLogger("websockets").setLevel(logging.WARNING)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
