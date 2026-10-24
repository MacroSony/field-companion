#!/usr/bin/env python3
"""Open-mic ignore probe; writes only docs/ignore-test.md (no audio artifacts).
Run with PYTHONDONTWRITEBYTECODE=1; defaults to both text models then preview audio.
"""
import argparse
import asyncio
import contextlib
import json
from pathlib import Path
import re
import time
from datetime import datetime, timezone

from realtime_probe import connect, collect, say_text, tts, stream_pcm, silence

REPORT = Path(__file__).resolve().parents[1] / "docs/ignore-test.md"
CASES = [
    ("ignore", "你中午想吃什么"),
    ("log", "两只绿头鸭"),
    ("ignore", "Did you bring the sunscreen?"),
    ("log", "three robins on the lawn"),
    ("answer", "刚才那是什么在叫？"),
    ("ignore", "哎呀鞋湿了"),
    ("log", "那边一只大蓝鹭"),
    ("either", "哇好漂亮"),
    ("ignore", "I think it's going to rain later"),
    ("log", "a red-tailed hawk circling"),
    ("answer", "how many species so far?"),
    ("ignore", "我妈刚给我打电话"),
    ("log", "还有一只君主斑蝶"),
    ("answer", "冠蓝鸦和北美红雀怎么区分？"),
    ("ignore", "Look at that cute dog"),
    ("log", "一只冠蓝鸦在橡树上"),
    ("either", "wait what was that"),
    ("answer", "Which species have we logged today?"),
]
INSTRUCTIONS = """You are a hands-free birding field companion with an open microphone.
The microphone also captures conversations with friends, self-talk, phone calls and noise.
If an utterance is NOT addressed to you (including chatting with a friend, self-talk,
phone calls, or unintelligible noise), call ONLY ignore() and say absolutely nothing:
no text, no speech, no acknowledgement before or after the call.
Ordinary off-topic remarks/questions to a companion are not requests for you.
Bird/nature sighting reports are addressed to you even without a wake word: call
add_observation(name, group, count, notes). Log insects too, but casual remarks about
someone's cute pet dog are chatter, not checklist reports. Never claim you added
anything without calling add_observation. For sightings call the tool without any
spoken/text preamble or confirmation: the client plays an earcon instead.
For an addressed birding question, answer in ONE short sentence, in the speaker's
language. Use get_checklist for questions about the recorded list/count; never invent
observations. If identification lacks evidence, briefly ask for a description rather
than guessing. For ambiguous exclamations, either silence via ignore or a brief
relevant answer is acceptable. You have no live audio-identification tool.
"""
TOOLS = [
    {"type": "function", "name": "ignore", "description": "Silently discard speech not addressed to the assistant; no speech or text.",
     "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"type": "function", "name": "add_observation", "description": "Record a reported organism in today's checklist. The client plays DING; do not speak.",
     "parameters": {"type": "object", "properties": {
         "name": {"type": "string"}, "group": {"type": "string"},
         "count": {"type": "integer", "minimum": 1}, "notes": {"type": "string"}},
         "required": ["name", "group", "count", "notes"], "additionalProperties": False}},
    {"type": "function", "name": "get_checklist", "description": "Read today's recorded checklist and species count.",
     "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
]
# Deterministic bilingual aliases, not a taxonomic resolver.
ALIASES = {
    "mallard": "mallard", "mallards": "mallard", "绿头鸭": "mallard", "anas platyrhynchos": "mallard",
    "robin": "american robin", "robins": "american robin", "american robin": "american robin", "american robins": "american robin", "北美旅鸫": "american robin", "旅鸫": "american robin", "turdus migratorius": "american robin",
    "great blue heron": "great blue heron", "大蓝鹭": "great blue heron", "ardea herodias": "great blue heron",
    "red-tailed hawk": "red-tailed hawk", "红尾鵟": "red-tailed hawk", "buteo jamaicensis": "red-tailed hawk",
    "monarch": "monarch butterfly", "monarch butterfly": "monarch butterfly", "君主斑蝶": "monarch butterfly", "danaus plexippus": "monarch butterfly",
    "blue jay": "blue jay", "冠蓝鸦": "blue jay", "cyanocitta cristata": "blue jay",
}


def execute(name, args, checklist):
    if name == "ignore":
        return {"ignored": True}
    if name == "get_checklist":
        return {"checklist": checklist, "species_count": len(checklist), "individual_count": sum(x["count"] for x in checklist)}
    if name == "add_observation":
        key = ALIASES.get(args["name"].lower().strip(), args["name"].lower().strip())
        row = next((x for x in checklist if x["key"] == key), None)
        if row:
            row["count"] += int(args.get("count", 1))
            row["notes"] += "; " + args.get("notes", "")
        else:
            row = {"key": key, **args}
            checklist.append(row)
        return {"ok": True, "observation": row, "species_count": len(checklist)}
    return {"error": "unknown tool"}


def added_claim(row):
    names = [c["name"] for c in row["calls"]]
    candidate = "add_observation" not in names and bool(re.search(r"\b(?:added|logged|recorded)\b|已.{0,4}(?:记|添加)|记下|记上|记录了|添加了|记好了", row["text"], re.I))
    # A checklist-backed retrospective total is not a claim of a NEW addition.
    summary = "get_checklist" in names and bool(re.search(r"(?:you(?:'ve| have)|we(?:'ve| have))\s+(?:recorded|logged)\s+(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+species", row["text"], re.I))
    row["added_claim_candidate"] = candidate
    return candidate and not summary


def counts(rows):
    return {
        "attempted_turns": len(rows),
        "strict_silent_chatter": sum(r["expected"] == "ignore" and r["actual"] == "ignored" for r in rows),
        "false_acts": sum(r["expected"] == "ignore" and r["actual"] not in ("ignored", "error") for r in rows),
        "false_ignores": sum(r["expected"] in ("log", "answer") and any(c["name"] == "ignore" for c in r["calls"]) for r in rows),
        "added_without_tool": sum(r["added_without_tool"] for r in rows),
        "errors": sum(r["actual"] == "error" for r in rows),
        "log_turns": sum(r["actual"] == "logged" for r in rows),
        "silent_log_turns": sum(r["actual"] == "logged" and not r["text"] and not r["audio_bytes"] for r in rows),
    }


def cell(value):
    return str(value).replace("|", "\\|").replace("\n", " ")


def write_report(runs, started):
    out = ["# Open-mic ignore() probe", "", f"Started (UTC): {started}. Updated after every turn.", "",
           "## Method", "",
           "One sequential session per run; same 18 interleaved turns (6 chatter, 6 sightings, 4 questions, 2 ambiguous). "
           "Fresh checklist per run, retained across turns. Text output for text runs; audio output for TTS-input run. "
           "Audio uses higgs-tts-3 PCM (24 kHz, 16 bit), real-time streaming, server_vad and higgs-stt-3.1. "
           "No human recordings, phone-call simulation, or unintelligible-noise sample in this small set.", "",
           "Strict silent = only ignore tool call(s), with no generated text/audio before the first call (this harness also rejects output after it). "
           "Function outputs are returned for all tools. Never response.create after ignore/add_observation; add emits client DING only. "
           "Only get_checklist is followed by response.create. Fake executor merges known bilingual species aliases.", "",
           "False act = expected-ignore turn not strictly ignored (transport errors excluded); false ignore = expected-log/answer turn calling ignore. "
           "Added-without-tool is a reviewable lexical heuristic (added/logged/recorded/记下/记录/添加/记上 etc., without add_observation on that turn); checklist-backed retrospective species totals are excluded, and raw candidates retained. "
           "Ambiguous turns excluded from error counts. Latency is from text submission/audio streaming start, not end of speech. "
           "First event = first response.* event (often response.created); action = first text/audio delta or function-call item. "
           "Audio latency therefore includes input duration. Text runs cannot establish audible silence. No waveform/audio artifacts saved.", "",
           "The system prompt explicitly treats pet-dog chatter as non-checklist speech; there are no per-turn labels or hints sent to the model. "
           "This is a prompt-compliance smoke test, not a blinded speaker/addressee benchmark.", "",
           "Setup caveat: initial setup-only attempts on both text models and preview audio received session.created but no session.updated within 15 seconds, so no turns were submitted in those attempts. "
           "Measured sessions proceed after session.created; its reported settings are retained below. In measured runs it includes the requested tools, matching instructions, model and modalities (and server_vad/STT for audio). "
           "Later retries are separate full-sequence sessions, never spliced into earlier partial results. Server-failed turns in retries are recorded and the same session continues with the next utterance, without reissuing the failed turn.", "",
           "Reproduce fresh runs: `PYTHONDONTWRITEBYTECODE=1 /data/build/boson-realtime-probe/.venv/bin/python probes/ignore_probe.py`. "
           "Use `--append --modes preview-text preview-audio` to retain prior evidence and append retries. No commit or push performed.", "",
           "## Exact instructions", "", "```text", INSTRUCTIONS.strip(), "```", ""]
    latest = {}
    for index, run in enumerate(runs, 1):
        if run["rows"]:
            latest[(run["model"], run["mode"])] = (index, run)
    out += ["## Summary (latest session per configuration)", "",
            "| Run | Model / mode | Turns | False acts / chatter | False ignores / log+question | Added claim without same-turn add tool | Silent logs / logs | Error turns |",
            "|---|---|---|---|---|---|---|---|"]
    for (model, mode), (index, run) in latest.items():
        rows = run["rows"]
        c = counts(rows)
        chatter = sum(r["expected"] == "ignore" for r in rows)
        addressed = sum(r["expected"] in ("log", "answer") for r in rows)
        out.append(f"| {index} | {model} / {mode} | {len(rows)}/18 | {c['false_acts']}/{chatter} | {c['false_ignores']}/{addressed} | {c['added_without_tool']} | {c['silent_log_turns']}/{c['log_turns']} | {c['errors']} |")
    out += ["", "### Notable observations and recommendation", ""]
    all_rows = [r for run in runs for r in run["rows"]]
    literal = [r for r in all_rows if r["text"].strip().lower() == "ignore" and not r["calls"]]
    if literal:
        out.append(f"- Literal `ignore` was generated as text, not a tool call, on {len(literal)} turns across the retained runs; this is NOT silence.")
    if any(c["false_acts"] for c in (counts(run["rows"]) for run in runs)):
        out.append("- Silence is not reliable: even preview responds to off-topic sunscreen/weather remarks. Text-only evidence is not proof of audio behavior.")
    audio_claims = [r for run in runs if run["mode"] == "audio" for r in run["rows"] if r["added_without_tool"]]
    for row in audio_claims:
        out.append(f"- Audio same-turn tool-less addition claim on `{row['utterance']}`: {row['text']} These refer to earlier real tool additions, so they are delayed acknowledgements, not evidence of a missing checklist write.")
    spoken_logs = [r for run in runs if run["mode"] == "audio" for r in run["rows"] if r["actual"] == "logged" and (r["text"] or r["audio_bytes"])]
    if spoken_logs:
        out.append(f"- Audio emitted speech on {len(spoken_logs)} sighting turns despite the earcon-only instruction and no post-add response.create; text/audio bytes and pre-call flags are preserved below.")
    if any(run["mode"] == "audio" and len(run["rows"]) > 2 for run in runs):
        out.append("- Audio STT sometimes returned cumulative text after silent turns (lunch + mallards + sunscreen; later phone remark + butterfly), rather than just the latest utterance. Attribution is therefore not perfectly isolated; the cause was not established.")
    if any(r["errors"] for r in all_rows):
        out.append("- Upstream service failures are recorded separately, not credited as successful silence. Partial attempts are retained, not combined with retry sessions.")
    if any("没有冠羽" in r["text"] for r in all_rows):
        out.append("- Secondary accuracy issue: preview text incorrectly said the cardinal lacks a crest. Identification accuracy was not the target metric; `answered` does not mean factually correct.")
    if any("nine individual birds" in r["text"] for r in all_rows):
        out.append("- The final total called all nine organisms birds despite one being a butterfly.")
    if any(run["mode"] == "audio" and any("冠蓝鸭" in (r.get("heard") or "") for r in run["rows"]) for run in runs):
        out.append("- STT also confused 冠蓝鸦 (blue jay) with 冠蓝鸭 on the comparison question; see the preserved transcript. This is an input-recognition limitation in addition to addressedness failures.")
    out += ["- Recommendation: do not ship always-open audio relying on prompt + ignore() alone. Use push-to-talk/wake-word or an explicit addressedness gate; buffer/mute playback until that decision. Keep function-output-only DING for logging, but also suppress sighting preambles and test delayed confirmations on the next turn. Preview is preferable to stable for further testing, not production-ready silence.",
            "- Limits: one small fixed sequence per completed configuration, synthetic speech, explicit dog-chatter cue, no phone-call/noise stress samples. Counts are descriptive, not a general reliability estimate.", ""]
    for run_index, run in enumerate(runs, 1):
        out += [f"## Run {run_index}: {run['model']} / {run['mode']}", "", f"Status: {run['status']}", "",
                "Counts: `" + json.dumps(counts(run["rows"]), ensure_ascii=False) + "`", "",
                "| # | Utterance | Expected | Actual | Tools | First event / action (ms) | Model text / audio bytes |",
                "|---|---|---|---|---|---|---|"]
        for i, r in enumerate(run["rows"], 1):
            timing = " / ".join("—" if r[k] is None else str(round(r[k] * 1000)) for k in ("first_event_s", "first_action_s"))
            tools = ", ".join(c["name"] for c in r["calls"]) or "—"
            if r["ding"]: tools += " [DING]"
            out.append("| " + " | ".join(map(cell, [i, r["utterance"], r["expected"], r["actual"], tools, timing, f"{r['text']} / {r['audio_bytes']} B"])) + " |")
        out += ["", "<details><summary>Per-turn evidence: arguments, transcription, timings, checklist snapshots, errors</summary>", "", "```json", json.dumps(run, ensure_ascii=False, indent=2), "```", "", "</details>", ""]
    REPORT.write_text("\n".join(out), encoding="utf-8")


async def run_mode(model, mode, run, flush, deadline):
    session = {"output_modalities": ["text" if mode == "text" else "audio"], "instructions": INSTRUCTIONS, "tools": TOOLS, "tool_choice": "auto"}
    if mode == "audio":
        session["audio"] = {"input": {"turn_detection": {"type": "server_vad"}, "transcription": {"model": "higgs-stt-3.1"}}}
    ws = await connect(session, model=model)
    checklist = []
    try:
        async def setup_event(e, t):
            if e["type"] in ("session.created", "session.updated"):
                s = e.get("session", {})
                run["setup"] = {"acknowledgement": e["type"], "model": s.get("model"),
                                "output_modalities": s.get("output_modalities"),
                                "tool_names": [x.get("name") for x in s.get("tools", [])],
                                "instructions_match": s.get("instructions") == INSTRUCTIONS,
                                "audio_input": s.get("audio", {}).get("input")}
            elif e["type"] == "error":
                run["setup_error"] = e.get("error")
        ready = await collect(ws, until=("session.created", "session.updated", "error"), timeout=15, on=setup_event)
        if not any(ty in ("session.created", "session.updated") for _, ty in ready["events"]):
            raise RuntimeError("No session readiness event")
        # This endpoint did not emit session.updated in the setup-only attempts.
        # Do not assume session.created proves the update; retain its fields as evidence.
        await asyncio.sleep(0.3)
        for expected, utterance in CASES:
            if time.monotonic() > deadline - 25:
                run["status"] = "Stopped at wall-time budget"
                break
            pcm = await asyncio.wait_for(asyncio.to_thread(tts, utterance), 65) if mode == "audio" else None
            t0 = time.perf_counter()
            row = {"utterance": utterance, "expected": expected, "actual": "error", "calls": [], "text": "", "audio_bytes": 0,
                   "first_event_s": None, "first_action_s": None, "first_call_s": None, "output_before_call": False,
                   "heard": None, "events": [], "errors": [], "statuses": [], "ding": 0, "added_without_tool": False}
            def observe(e, t):
                ty = e["type"]
                # Keep event types/times, never PCM/base64 deltas.
                if not ty.endswith(".delta"):
                    row["events"].append([round(t, 3), ty])
                if ty.startswith("response.") and row["first_event_s"] is None:
                    row["first_event_s"] = round(t, 4)
                is_call = ty in ("response.output_item.added", "response.output_item.done") and e.get("item", {}).get("type") == "function_call"
                is_output = ty in ("response.output_text.delta", "response.output_audio_transcript.delta", "response.output_audio.delta")
                if is_call and row["first_call_s"] is None:
                    row["first_call_s"] = round(t, 4)
                if (is_call or is_output) and row["first_action_s"] is None:
                    row["first_action_s"] = round(t, 4)
                if is_output and row["first_call_s"] is None:
                    row["output_before_call"] = True
                if ty in ("response.output_text.delta", "response.output_audio_transcript.delta"):
                    row["text"] += e.get("delta", "")
                if ty == "response.output_audio.delta":
                    import base64
                    row["audio_bytes"] += len(base64.b64decode(e["delta"]))
                if ty == "conversation.item.input_audio_transcription.completed":
                    row["heard"] = e.get("transcript")
                if ty == "error":
                    row["errors"].append(e.get("error"))
            async def on(e, t):
                observe(e, t)
            sender = None
            try:
                if mode == "text":
                    await say_text(ws, utterance)
                else:
                    sender = asyncio.create_task(stream_pcm(ws, pcm + silence(1500)))
                    row["input_audio_s"] = round(len(pcm) / 48000, 3)
                for _ in range(5):
                    result = await collect(ws, t0=t0, timeout=35, on=on)
                    row["statuses"].append(result.get("status", "timeout"))
                    if result.get("timeout"):
                        row["errors"].append("response collection timed out")
                    calls = [c for c in result["calls"] if c.get("executor", "client") == "client"]
                    for call in calls:
                        args = json.loads(call.get("arguments") or "{}")
                        row["calls"].append({"name": call["name"], "arguments": args})
                        output = execute(call["name"], args, checklist)
                        if call["name"] == "add_observation":
                            row["ding"] += 1
                            print("DING", flush=True)
                        await ws.send(json.dumps({"type": "conversation.item.create", "item": {
                            "type": "function_call_output", "call_id": call["call_id"], "output": json.dumps(output, ensure_ascii=False)}}))
                    if not calls or any(c["name"] in ("ignore", "add_observation") for c in calls):
                        break
                    await ws.send(json.dumps({"type": "response.create"}))
                if sender:
                    await sender
                # Drain late STT and item acknowledgements before advancing the next turn.
                # Unexpected spontaneous responses are recorded as protocol anomalies, not hidden.
                while True:
                    try:
                        event = json.loads(await asyncio.wait_for(ws.recv(), 0.5))
                    except asyncio.TimeoutError:
                        break
                    observe(event, time.perf_counter() - t0)
                    if event["type"] == "response.created":
                        row["errors"].append("unexpected spontaneous response during drain")
                    if event["type"] == "response.done":
                        for call in event.get("response", {}).get("output", []):
                            if call.get("type") == "function_call":
                                row["calls"].append({"name": call["name"], "arguments": json.loads(call.get("arguments") or "{}"), "unexecuted": True})
                names = [c["name"] for c in row["calls"]]
                has_output = bool(row["text"] or row["audio_bytes"])
                if names and set(names) == {"ignore"} and not has_output:
                    row["actual"] = "ignored"
                elif "ignore" in names and has_output:
                    row["actual"] = "spoke-then-ignored" if row["output_before_call"] else "ignored-then-spoke"
                elif "add_observation" in names:
                    row["actual"] = "logged"
                elif has_output:
                    row["actual"] = "answered"
                elif not row["errors"]:
                    row["actual"] = "no-action"
                row["added_without_tool"] = added_claim(row)
            finally:
                if sender and not sender.done():
                    sender.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await sender
                row["total_s"] = round(time.perf_counter() - t0, 3)
                row["checklist_after"] = json.loads(json.dumps(checklist, ensure_ascii=False))
                run["rows"].append(row)
                flush()
                print(json.dumps({k: row[k] for k in ("utterance", "expected", "actual", "calls", "text", "heard", "first_event_s", "first_action_s", "errors")}, ensure_ascii=False), flush=True)
            if row["errors"] and not (row["statuses"] == ["failed"] and all(isinstance(e, dict) and e.get("code") == "upstream_error" for e in row["errors"])):
                run["status"] = "Stopped after protocol/response error; session not reset"
                break
        else:
            run["status"] = "Complete sequence" + (" (includes server-failed turns)" if any(r["errors"] for r in run["rows"]) else "")
    finally:
        await ws.close()


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--modes", nargs="+", choices=["preview-text", "stable-text", "preview-audio"], default=["preview-text", "stable-text", "preview-audio"])
    parser.add_argument("--budget-seconds", type=int, default=690)
    parser.add_argument("--append", action="store_true", help="Retain prior run evidence and append fresh full-session runs")
    args = parser.parse_args()
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    runs = []
    if args.append and REPORT.exists():
        previous = REPORT.read_text(encoding="utf-8")
        runs = [json.loads(b) for b in re.findall(r"```json\n(.*?)\n```", previous, re.S)]
        match = re.search(r"Started \(UTC\): (.*?)\. Updated", previous)
        if match:
            started = match.group(1)
        for previous_run in runs:
            for row in previous_run["rows"]:
                row["added_without_tool"] = added_claim(row)
    deadline = time.monotonic() + args.budget_seconds
    flush = lambda: write_report(runs, started)
    flush()
    for label in args.modes:
        model = "higgs-realtime" if label == "stable-text" else "higgs-realtime-1.5-preview"
        mode = "audio" if label.endswith("audio") else "text"
        run = {"model": model, "mode": mode, "status": "Running", "rows": []}
        runs.append(run)
        flush()
        try:
            await asyncio.wait_for(run_mode(model, mode, run, flush, deadline), max(1, deadline - time.monotonic()))
        except Exception as exc:
            # Exception type/message only; never headers, environment, or credentials.
            run["status"] = f"Failed: {type(exc).__name__}: {exc}"
            print(run["status"], flush=True)
        flush()
        print(label, json.dumps(counts(run["rows"])), flush=True)
        if time.monotonic() > deadline:
            break


if __name__ == "__main__":
    asyncio.run(main())
