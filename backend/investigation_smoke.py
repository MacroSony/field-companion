"""Opt-in live voice/expert check, then a simulated 61-second investigation.

Uses typed input and synthetic evidence; consumes generated PCM without playback.
One real GPT request plus real Higgs turns. The long delay uses a fixture expert.
"""
import argparse
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import time
import uuid

import server as s


async def main(delay, report_path):
    events, routes, rows = [], [], []
    audio = 0

    class Phone:
        async def send_json(self, event):
            events.append(event)

        async def send_bytes(self, pcm):
            nonlocal audio
            audio += len(pcm)

    c = s.Session("background-smoke-" + uuid.uuid4().hex[:8])
    client = s.Client(Phone(), c)
    c.clients.add(client)
    c.mic, c.mic_on = client, False
    c.allowed_species = lambda: set()
    obs = s.Observation("synthetic-original", fused=[
        {"sci": "Dryobates pubescens", "en": "Downy Woodpecker", "p": .55},
        {"sci": "Leuconotopicus villosus", "en": "Hairy Woodpecker", "p": .45}],
        detections=[s.Detection("synthetic-frame", "image", s.now_iso())])
    c.observations[obs.id], c.focus = obs, obs.id

    def log(event, **fields):
        if event == "route_done":
            routes.append(fields["tools"])
        if event in ("turn_failed", "realtime_disconnected", "tool_failed", "realtime_error", "response_done"):
            print(json.dumps({"notice": event, **fields}), flush=True)

    c.log = log

    async def until(predicate, seconds=45):
        async with asyncio.timeout(seconds):
            while not predicate():
                await asyncio.sleep(.025)

    async def turn(text, expected):
        # Typed input is an explicit new turn. Flush unplayed test PCM between
        # turns rather than pretending that audio was played on a real speaker.
        await c.handle_phone({"type": "tap"})
        before_routes, before_events, before_audio = len(routes), len(events), audio
        started = time.monotonic()
        await c.handle_phone({"type": "text", "text": text})
        await until(lambda: len(routes) > before_routes and not c.active)
        row = {"question": text, "tool": routes[-1], "elapsed_ms": round((time.monotonic() - started) * 1000),
               "answer": " ".join(e["text"] for e in events[before_events:]
                   if e.get("type") == "transcript" and e.get("role") == "assistant" and e.get("final")),
               "audio_bytes": audio - before_audio,
               "jobs": [{"id": j.id, "status": j.status} for j in c.investigations.jobs.values()]}
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
        assert routes[-1] == [expected], row
        return row

    c.start()
    fixture_calls = []
    try:
        if not c.expert.available:
            raise RuntimeError("The existing expert credential is not configured")
        await until(lambda: c.connected)
        await turn("它吃什么？", "ask_expert")
        live = c.investigations.jobs["R1"]
        await until(lambda: live.status not in ("queued", "running"), 140)
        assert live.status == "completed", live.result.get("status")
        assert c.chinese(live.result["answer"])
        assert live.result["source_ids"], live.result
        await until(lambda: live.announced and not c.active)
        print(json.dumps({"real_gpt_result": live.payload()}, ensure_ascii=False), flush=True)

        async def fixture(question, snapshot, *, on_progress, **kwargs):
            fixture_calls.append(question)
            wait = delay if len(fixture_calls) == 1 else .1
            await on_progress("retrieving")
            await asyncio.sleep(wait / 2)
            await on_progress("analyzing")
            await asyncio.sleep(wait / 2)
            return {"ok": True, "status": "completed", "action": "request_more_evidence",
                    "answer": "This simulated long investigation is complete. Please photograph the original bird's bill.",
                    "hint": "Original bird's bill", "evidence_ids": ["synthetic-frame"],
                    "source_ids": [], "source_quotes": [], "sources": []}

        c.expert = SimpleNamespace(available=True, answer=fixture)
        await turn("Could it be a Hairy Woodpecker?", "ask_expert")
        first = c.investigations.jobs["R2"]
        started = time.monotonic()
        checklist = await turn("How many species today?", "get_checklist")
        assert checklist["audio_bytes"] > 0
        await turn("How is investigation R2 going?", "get_investigations")
        await turn("What habitat does it live in?", "ask_expert")
        assert c.investigations.jobs["R3"].status == "queued"
        await turn("Cancel investigation R3.", "manage_investigation")
        assert c.investigations.jobs["R3"].status == "cancelled"
        await turn("Start another investigation into its nesting behavior.", "ask_expert")
        assert c.investigations.jobs["R4"].status == "queued"
        assert first.status == "running", "Fixture finished before interactive checks"
        interactive_seconds = round(time.monotonic() - started, 2)
        await c.handle_phone({"type": "mode", "quiet": True})
        await c.new_focus("Insecta")
        await until(lambda: c.investigations.jobs["R4"].status == "completed", delay + 20)
        assert first.status == "completed" and first.elapsed_seconds >= delay - .1
        assert len(fixture_calls) == 2
        assert not first.announced
        assert c.observations[c.focus].hint == ""
        before_audio = audio
        await c.handle_phone({"type": "mode", "quiet": False})
        await until(lambda: first.announced and c.investigations.jobs["R4"].announced and not c.active)
        assert audio > before_audio
        assert c.items == []
        report = {"ok": True, "real_higgs": True, "real_gpt_first_task": True,
                  "synthetic_evidence": True, "physical_media": False,
                  "simulated_long_task_seconds": round(first.elapsed_seconds, 2),
                  "interactive_checks_seconds": interactive_seconds, "turns": rows,
                  "jobs": c.investigations.payload(), "audio_bytes": audio}
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"ok": True, "report": str(report_path),
                          "long_task_seconds": report["simulated_long_task_seconds"]}), flush=True)
    finally:
        await c.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--slow-seconds", type=float, default=61)
    parser.add_argument("--report", type=Path, default=s.ROOT / "out/investigation-checks/live-results.json")
    args = parser.parse_args()
    if not 45 <= args.slow_seconds <= 180:
        parser.error("--slow-seconds must be between 45 and 180")
    asyncio.run(main(args.slow_seconds, args.report))
