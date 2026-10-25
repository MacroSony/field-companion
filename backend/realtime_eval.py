"""Opt-in paired Realtime benchmark; real APIs, synthetic context, no tool effects.

Each sample gets a fresh WebSocket/conversation. Both providers receive identical
instructions, tools, history and input. Keys are read in memory, never reported.
Run from the repo: python backend/realtime_eval.py --rounds 2 --report out/realtime-eval/text.json
"""
import argparse
import asyncio
import base64
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import random
import re
import statistics
import time
import wave

import numpy as np
from scipy.signal import resample_poly
import websockets

import server as backend
from expert import Settings
from investigations import Investigation
from realtime_provider import RealtimeConfig, local_values
from realtime_eval_cases import CASES, CARDINAL, ROBIN, public_case
from route_safety import route_guard

FIXED_TIME = "2026-10-24T12:00:00+00:00"
MISSING = object()


def normalized(value):
    return " ".join(re.findall(r"\w+", str(value).casefold()))


def matches(actual, expected):
    """Subset comparison with explicit default/alias/unordered alternatives."""
    if isinstance(expected, dict):
        if "$absent" in expected:
            return actual is MISSING
        if "$default" in expected:
            return actual is MISSING or matches(actual, expected["$default"])
        if "$in" in expected:
            return actual is not MISSING and any(matches(actual, v) for v in expected["$in"])
        if "$contains" in expected:
            return isinstance(actual, str) and normalized(expected["$contains"]) in normalized(actual)
        if "$all_pending" in expected:
            # Omitted species means all. Explicitly listing both is equivalent.
            return actual is MISSING or actual == [] or matches(actual, {"$unordered": [CARDINAL, ROBIN]})
        if "$unordered" in expected:
            wanted = expected["$unordered"]
            if not isinstance(actual, list) or len(actual) != len(wanted):
                return False
            # Backtracking avoids greedy alias matches hiding a valid pairing.
            def pair(rest, values):
                return not rest or any(matches(v, rest[0]) and pair(rest[1:], values[:i] + values[i+1:])
                                       for i, v in enumerate(values))
            return pair(wanted, actual)
        return isinstance(actual, dict) and all(matches(actual.get(k, MISSING), v) for k, v in expected.items())
    if actual is MISSING:
        return False
    if isinstance(expected, str):
        return isinstance(actual, str) and normalized(actual) == normalized(expected)
    return type(actual) is type(expected) and actual == expected


def valid_schema(value, schema):
    """The subset of JSON Schema used by the app's tool definitions."""
    kind = schema.get("type")
    types = {"object": dict, "array": list, "string": str, "boolean": bool, "integer": int}
    if kind == "number":
        if type(value) not in (int, float) or not math.isfinite(value):
            return False
    elif kind in types and type(value) is not types[kind]:
        return False
    if "enum" in schema and value not in schema["enum"]:
        return False
    if kind in ("number", "integer"):
        return schema.get("minimum", -math.inf) <= value <= schema.get("maximum", math.inf)
    if kind == "object":
        if not all(k in value for k in schema.get("required", [])):
            return False
        properties = schema.get("properties", {})
        additional = schema.get("additionalProperties")
        return all(valid_schema(v, properties[k]) if k in properties else
                   valid_schema(v, additional) if isinstance(additional, dict) else additional is not False
                   for k, v in value.items())
    if kind == "array":
        return len(value) >= schema.get("minItems", 0) and all(valid_schema(v, schema.get("items", {})) for v in value)
    return True


def fixture(case, config):
    """Build real Session snapshots without starting any background workers."""
    session = backend.Session("paired-eval", realtime=config)
    session.log = lambda *args, **kwargs: None
    session.expert.settings = Settings(enabled=False)
    if case.state in ("checklist",):
        obs = backend.Observation("logged-mallard", status="confirmed", count=2, uncertain=False,
            t_start=FIXED_TIME, fused=[{"sci": "Anas platyrhynchos", "en": "Mallard", "p": 1.0}])
        session.observations[obs.id] = obs
    if case.state.startswith("photo") or case.state in ("research", "research-ready", "research-failed", "explanation-offer", "mixed", "retry-mixed", "retry-photo-offer", "retry-selection"):
        obs = backend.Observation("photo-eagle", t_start=FIXED_TIME,
            fused=[{"sci": "Haliaeetus leucocephalus", "en": "Bald Eagle", "p": .9}],
            uncertain=False, analysis_status="ready", analysis_stage="ready",
            analysis_request_id="capture-1", analysis_created_at=FIXED_TIME, image_revision=1,
            detections=[backend.Detection("image-1", "image", FIXED_TIME, [
                {"sci": "Haliaeetus leucocephalus", "en": "Bald Eagle", "p": .9}])])
        if case.state in ("photo-running", "mixed"):
            obs.fused = []  # The current image has not been classified yet.
            obs.analysis_status, obs.analysis_stage = "analyzing", "recognizing"
            session.camera_request = {"id": "capture-1", "obs": obs.id, "phase": "classifying", "language": "en"}
        if case.state in ('photo-failed', 'retry-mixed', 'retry-photo-offer', 'retry-selection'):
            obs.fused = []
            obs.analysis_status = obs.analysis_stage = 'failed'
            session.camera_request = {'id':'capture-1','obs':obs.id,'phase':'ready','confirmed':False,'language':'en'}
        session.observations[obs.id] = obs
        session.focus = obs.id
    if case.state.startswith("research") or case.state in ("mixed", "explanation-offer", "retry-mixed", "retry-photo-offer", "retry-selection"):
        ready = case.state in ("research-ready", "explanation-offer")
        failed = case.state in ('research-failed','retry-mixed','retry-photo-offer','retry-selection')
        for i in range(1, 3):
            job = Investigation(f"R{i}", "What does the original bird eat?" if i == 1 else "Where does it migrate?",
                {"id": "research-original", "fused": [{"sci": "Cyanocitta cristata", "en": "Blue Jay"}]},
                status="completed" if ready else "failed" if failed else "running" if i == 1 else "queued",
                stage="completed" if ready else "failed" if failed else "retrieving" if i == 1 else "queued",
                created_at=f"2026-10-24T11:0{i}:00+00:00")
            if ready:
                job.result = {"ok": True, "answer": "Fixture findings; not exposed to the router."}
            session.investigations.jobs[job.id] = job
        if case.state == "explanation-offer":
            session.investigation_offer_ids = ["R1"]
    if case.state == "suggestions":
        for sci, en in (("Cardinalis cardinalis", "Northern Cardinal"), ("Turdus migratorius", "American Robin")):
            session.suggestions[sci] = {"sci": sci, "en": en, "conf": .9, "photo": "", "t": FIXED_TIME}
            session.history.append((0, {"sci": sci, "en": en, "conf": .9}))
    session.last_assistant_text = next((text for role, text in reversed(case.history) if role == "assistant"), "")
    if case.state in ('retry-photo-offer','retry-selection'):
        import time
        rows=session.task_status_snapshot()
        if case.state=='retry-photo-offer':
            rows=[row for row in rows if row['kind']=='camera']
        session.retry_offer={'question':session.last_assistant_text,'tasks':[
            {'id':row['id'],'kind':row['kind']} for row in rows],'at':time.monotonic()}
    return session


def grade(case, response, route_text="", audio_bytes=0, *, consent=None, input_text=None):
    calls = [item for item in response.get("output", []) if item.get("type") == "function_call"]
    parsed = []
    schema_by_name = {t["name"]: t["parameters"] for t in backend.TOOLS}
    for item in calls:
        try:
            arguments = json.loads(item.get("arguments", ""))
        except (ValueError, TypeError):
            arguments = None
        parsed.append({"tool": item.get("name"), "arguments": arguments})
    schema_ok = len(parsed) == 1 and parsed[0]["tool"] in schema_by_name and valid_schema(
        parsed[0]["arguments"], schema_by_name[parsed[0]["tool"]])
    tool_ok = len(parsed) == 1 and any(parsed[0]["tool"] == e["tool"] for e in case.expected)
    arguments_ok = schema_ok and any(p["tool"] == e["tool"] and matches(p["arguments"], e["arguments"])
                                     for p in parsed for e in case.expected)
    forbidden = [p["tool"] for p in parsed if p["tool"] in case.forbidden]
    # This is an audit of the existing guard, not execution or a second grader.
    guards = [dict(zip(("decision", "reason"), route_guard(p["tool"], p["arguments"],
                case.text if input_text is None else input_text,
                suggestions=case.state == "suggestions", **(consent or {}))))
              for p in parsed if isinstance(p["arguments"], dict)]
    final_text = " ".join(str(part.get("text") or part.get("transcript") or "")
                          for item in response.get("output", []) if item.get("type") == "message"
                          for part in item.get("content", []))
    silent = not (route_text.strip() or final_text.strip()) and audio_bytes == 0
    return {"passed": response.get("status") == "completed" and arguments_ok and silent,
            "tool_correct": tool_ok, "arguments_correct": bool(arguments_ok), "schema_valid": bool(schema_ok),
            "route_silent": silent, "calls": parsed, "forbidden_attempts": forbidden, "guard": guards}


class UpstreamError(RuntimeError):
    pass


class AudioTranscripts:
    """Join split VAD turns in audio order, even when STT finishes out of order."""
    def __init__(self):
        self.turns = {}

    def feed(self, event):
        ty, item_id = event.get("type"), event.get("item_id")
        if not item_id or ty not in ("input_audio_buffer.speech_started", "input_audio_buffer.speech_stopped",
                "input_audio_buffer.committed", "conversation.item.input_audio_transcription.completed"):
            return
        row = self.turns.setdefault(item_id, {"item_id": item_id, "order": len(self.turns)})
        if ty == "input_audio_buffer.speech_started":
            row["audio_start_ms"] = event.get("audio_start_ms", row["order"])
        if ty == "conversation.item.input_audio_transcription.completed":
            row["transcript"] = event.get("transcript", "")

    @property
    def complete(self):
        return bool(self.turns) and all("transcript" in r for r in self.turns.values())

    def segments(self):
        return sorted(self.turns.values(), key=lambda r: (r.get("audio_start_ms", math.inf), r["order"]))

    def text(self):
        return " ".join(r["transcript"].strip() for r in self.segments() if r.get("transcript", "").strip())


async def collect_audio_transcripts(receive, collector, *, timeout=20, settle=.6):
    """Drain all queued events and wait for known VAD turns, not first STT only."""
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Incomplete fixture transcripts")
        try:
            event = await asyncio.wait_for(receive(), min(settle if collector.complete else 1, remaining))
            collector.feed(event)
        except TimeoutError:
            if collector.complete:
                return collector.text()


def speech_errors(reference, transcript):
    # Chinese characters are separate tokens; English words ignore punctuation.
    # This intentionally does not equate spelled-out numbers with digits.
    tokenize = lambda text: re.findall(r"[\u3400-\u9fff]|[a-z0-9']+", text.casefold())
    left, right = tokenize(reference), tokenize(transcript)
    previous = list(range(len(right) + 1))
    for i, a in enumerate(left, 1):
        current = [i]
        for j, b in enumerate(right, 1):
            current.append(min(current[-1] + 1, previous[j] + 1, previous[j-1] + (a != b)))
        previous = current
    return {"reference_tokens": len(left), "edit_errors": previous[-1],
            "exact_normalized": left == right}


async def run_sample(case, config, audio_dir=None):
    session = fixture(case, config)
    common = session.config("route")
    shared = {k: common[k] for k in ("instructions", "tools", "tool_choice", "output_modalities")}
    digest = lambda value: hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    result = {"case": case.id, "category": case.category, "provider": config.provider, "model": config.model,
              "transcription_model": config.transcription_model,
              "input": case.text, "state": case.state, "prompt_sha256": digest(shared),
              "history_sha256": digest(case.history), "attempts": [], "passed": False}
    consent = session.consent_context()
    try:
        key = config.openai_key() if config.provider == "openai" else backend.api_key()
        for attempt in range(2):  # Same one-retry budget as production routing.
            audit = {"retry": attempt, "route_text": "", "audio_bytes": 0, "api_errors": []}
            result["attempts"].append(audit)
            started = None
            try:
                async with websockets.connect(config.url, additional_headers={"Authorization": "Bearer " + key},
                        open_timeout=20, max_size=None, ping_interval=20, ping_timeout=20) as ws:
                    async def send(event):
                        await ws.send(json.dumps(event, ensure_ascii=False))

                    async def receive():
                        event = json.loads(await ws.recv())
                        ty = event.get("type", "")
                        if ty == "error":
                            safe = {k: event.get("error", {}).get(k) for k in ("type", "code")}
                            message = str(event.get("error", {}).get("message", "")).replace(key, "[REDACTED]")
                            safe["message"] = re.sub(r"sk-[\w-]+", "[REDACTED]", message)[:4000]
                            audit["api_errors"].append(safe)
                            raise UpstreamError("Realtime protocol error")
                        if ty in ("response.output_text.delta", "response.text.delta", "response.output_audio_transcript.delta", "response.audio_transcript.delta"):
                            audit["route_text"] += event.get("delta", "")
                        if ty in ("response.output_audio.delta", "response.audio.delta"):
                            audit["audio_bytes"] += len(base64.b64decode(event.get("delta", "")))
                        if ty == "conversation.item.input_audio_transcription.failed":
                            raise UpstreamError("Input transcription failed")
                        return event

                    async def until(predicate, timeout):
                        async with asyncio.timeout(timeout):
                            while True:
                                event = await receive()
                                if predicate(event):
                                    return event

                    initial = session.session()
                    await send({"type": "session.update", "session": initial})
                    await until(lambda e: config.accepts_initial(e, initial), 20)
                    for role, text in case.history:
                        await send({"type": "conversation.item.create", "item": {"type": "message", "role": role,
                            "content": [{"type": ("output_text" if config.provider == "openai" else "text")
                                         if role == "assistant" else "input_text", "text": text}]}})
                    if audio_dir is None:
                        await send({"type": "conversation.item.create", "item": {"type": "message", "role": "user",
                            "content": [{"type": "input_text", "text": case.text}]}})
                    else:
                        path = audio_dir / (case.id + ".wav")
                        with wave.open(str(path), "rb") as source:
                            if (source.getnchannels(), source.getsampwidth(), source.getframerate()) != (1, 2, 48000):
                                raise ValueError("Expected 48 kHz mono PCM16 fixture")
                            pcm = source.readframes(source.getnframes())
                        result["audio_sha256"] = hashlib.sha256(pcm).hexdigest()
                        values = np.frombuffer(pcm, "<i2").astype(np.float32) / 32768
                        converted = (np.clip(resample_poly(values, 1, 2), -1, 1) * 32767).astype("<i2").tobytes() + bytes(48000)
                        begin = time.monotonic()
                        for offset in range(0, len(converted), 1920):
                            await asyncio.sleep(max(0, begin + offset / 48000 - time.monotonic()))
                            await send({"type": "input_audio_buffer.append", "audio": base64.b64encode(converted[offset:offset + 1920]).decode()})
                        collector = AudioTranscripts()
                        audit["transcript"] = await collect_audio_transcripts(receive, collector)
                        audit["transcript_segments"] = collector.segments()
                        # Use actual STT, never the known script. A fixture may have
                        # several VAD segments: join them in audio order for grading.
                        await send({"type": "conversation.item.create", "item": {"type": "message", "role": "user",
                            "content": [{"type": "input_text", "text": audit["transcript"]}]}})
                    started = time.monotonic()
                    await send({"type": "session.update", "session": common})
                    await until(lambda e: e.get("type") == "session.updated" and all(
                        e.get("session", {}).get(k) == common[k] for k in ("instructions", "tool_choice", "output_modalities")), 12)
                    created = time.monotonic()
                    await send({"type": "response.create"})
                    response = (await until(lambda e: e.get("type") == "response.done", 8))["response"]
                    audit["route_ms"] = round((time.monotonic() - started) * 1000, 1)
                    audit["generation_ms"] = round((time.monotonic() - created) * 1000, 1)
                    audit["response"] = response
                    if response.get("status") != "completed":
                        raise UpstreamError("Generation did not complete")
                    result.update(grade(case, response, audit["route_text"], audit["audio_bytes"], consent=consent,
                                        input_text=audit.get("transcript")))
                    if audio_dir is not None:
                        result["speech"] = speech_errors(case.text, audit["transcript"])
                    result["technical_success"] = True
                    result.pop("error_type", None)
                    result["route_ms"] = sum(a.get("route_ms", 0) for a in result["attempts"])
                    result["retry_count"] = attempt
                    break
            except (TimeoutError, OSError, websockets.WebSocketException, UpstreamError, RuntimeError) as exc:
                if started is not None and "route_ms" not in audit:
                    audit["route_ms"] = round((time.monotonic() - started) * 1000, 1)
                audit["error_type"] = type(exc).__name__  # Exception strings can contain headers.
                result["technical_success"] = False
                result["error_type"] = type(exc).__name__
                if attempt == 0:
                    await asyncio.sleep(.3)
        return result
    finally:
        await session.close()


def summarize(samples):
    summary = {}
    for provider in sorted({r["provider"] for r in samples}):
        rows = [r for r in samples if r["provider"] == provider]
        completed = [r for r in rows if r.get("technical_success")]
        latency = sorted(r["route_ms"] for r in completed)
        categories = {}
        for category in sorted({r["category"] for r in rows}):
            group = [r for r in rows if r["category"] == category]
            categories[category] = {"passed": sum(r["passed"] for r in group), "total": len(group),
                                    "action_correct": sum(r.get("arguments_correct", False) for r in group)}
        summary[provider] = {"model": rows[0]["model"], "samples": len(rows), "completed": len(completed),
            "passed": sum(r["passed"] for r in rows),
            "tool_correct": sum(r.get("tool_correct", False) for r in rows),
            "arguments_correct": sum(r.get("arguments_correct", False) for r in rows),
            "schema_errors": sum(not r["schema_valid"] for r in completed),
            "forbidden_attempts": sum(bool(r.get("forbidden_attempts")) for r in rows),
            "guard_interceptions": sum(any(g["decision"] != "allow" for g in r.get("guard", [])) for r in rows),
            "route_speech": sum(not r["route_silent"] for r in completed),
            "retries": sum(len(r["attempts"]) - 1 for r in rows),
            "route_median_ms": round(statistics.median(latency), 1) if latency else None,
            "route_p95_ms": latency[max(0, math.ceil(len(latency) * .95) - 1)] if latency else None,
            "categories": categories}
        speech = [r["speech"] for r in completed if "speech" in r]
        if speech:
            tokens = sum(r["reference_tokens"] for r in speech)
            errors = sum(r["edit_errors"] for r in speech)
            summary[provider]["speech"] = {"samples": len(speech), "reference_tokens": tokens,
                "edit_errors": errors, "token_error_rate": round(errors / tokens, 4) if tokens else None,
                "exact_normalized": sum(r["exact_normalized"] for r in speech)}
    return summary


async def main(args):
    local = local_values()
    import os
    value = lambda name, default: os.environ.get(name) or local.get(name) or default
    configs = {"higgs": RealtimeConfig(), "openai": RealtimeConfig(provider="openai",
        model=value("OPENAI_REALTIME_MODEL", "gpt-realtime-2.1"),
        transcription_model=value("OPENAI_REALTIME_TRANSCRIPTION_MODEL", "gpt-transcribe"),
        voice=value("OPENAI_REALTIME_VOICE", "marin"))}
    selected = [c for c in CASES if (not args.cases or c.id in args.cases) and (not args.audio_dir or c.audio)]
    if not selected or (args.cases and set(args.cases) - {c.id for c in selected}):
        raise ValueError("No cases selected or unknown case IDs")
    if args.rounds < 1:
        raise ValueError("rounds must be positive")
    if len(set(args.providers)) != len(args.providers):
        raise ValueError("providers must be unique")
    if args.audio_dir:
        for case in selected:
            with wave.open(str(args.audio_dir / (case.id + ".wav")), "rb") as source:
                if (source.getnchannels(), source.getsampwidth(), source.getframerate()) != (1, 2, 48000):
                    raise ValueError("Expected 48 kHz mono PCM16 fixtures")
    args.report.parent.mkdir(parents=True, exist_ok=True)
    report = {"created_at": datetime.now(timezone.utc).isoformat(), "mode": "synthetic-audio" if args.audio_dir else "text",
        "rounds": args.rounds, "seed": args.seed, "cases": [public_case(c) for c in selected],
        "method": {"fresh_conversation_per_sample": True, "tool_execution": False,
                   "production_prompts_and_tools": True, "guard_execution": "audit only",
                   "route_timeout_seconds": 8, "retry_budget": 1,
                   "retry_transport": "fresh connection; production normally reuses its connection",
                   "audio_turn_collection": "all known VAD turns in audio order, after 600ms event silence; not app timing",
                   "latency": "session route update + generation; excludes connection, speech duration, and retry connection setup",
                   "limitations": ["synthetic contexts and inputs", "no phone echo/noise", "no answer-quality grading",
                                   "no actual BirdNET/BioCLIP/iNaturalist/expert execution", "default deployed provider settings"]},
        "samples": []}
    def save():
        report["summary"] = summarize(report["samples"])
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    save()  # Freeze expected answers before the first live API request.
    consecutive_failures = {provider: 0 for provider in args.providers}
    for round_index in range(args.rounds):
        cases = list(selected)
        random.Random(args.seed + round_index).shuffle(cases)
        for index, case in enumerate(cases):
            providers = list(args.providers)
            if (round_index + index) % 2:
                providers.reverse()
            for provider in providers:
                sample = await run_sample(case, configs[provider], args.audio_dir)
                sample["round"] = round_index + 1
                report["samples"].append(sample)
                save()
                print(json.dumps({"round": round_index + 1, "case": case.id, "provider": provider,
                    "passed": sample["passed"], "calls": sample.get("calls"), "ms": sample.get("route_ms"),
                    "error_type": sample.get("error_type")}, ensure_ascii=False), flush=True)
                consecutive_failures[provider] = 0 if sample.get("technical_success") else consecutive_failures[provider] + 1
                if consecutive_failures[provider] >= 3:
                    report["aborted"] = {"provider": provider, "reason": "three consecutive technical failures"}
                    report["pairing_valid"] = False
                    save()
                    return 2
    # Assert pairing instead of accidentally publishing scores from unequal prompts.
    groups = {}
    for sample in report["samples"]:
        groups.setdefault((sample["round"], sample["case"]), []).append(sample)
    report["pairing_valid"] = all(len(rows) == len(args.providers) and len({
        (r["prompt_sha256"], r["history_sha256"], r.get("audio_sha256")) for r in rows}) == 1 for rows in groups.values())
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    save()
    print(json.dumps({"report": str(args.report), "pairing_valid": report["pairing_valid"], "summary": report["summary"]}), flush=True)
    return 0 if report["pairing_valid"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--providers", nargs="+", choices=("higgs", "openai"), default=["higgs", "openai"])
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--seed", type=int, default=24)
    parser.add_argument("--cases", nargs="+")
    parser.add_argument("--audio-dir", type=Path, help="Optional identical local SAPI WAV fixtures; only audio-marked cases")
    parser.add_argument("--report", type=Path, default=Path("out/realtime-eval/text.json"))
    raise SystemExit(asyncio.run(main(parser.parse_args())))
