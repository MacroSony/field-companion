"""Render public, synthetic benchmark evidence from a local paired-run JSON."""
import argparse
import json
from pathlib import Path

from realtime_eval import grade, fixture, summarize
from realtime_eval_cases import CASES, public_case
from realtime_provider import RealtimeConfig


async def regrade(report):
    """Recompute deterministic grades after scorer fixes, retaining originals.

    Refuse changed inputs/fixtures/history. This never calls a model or changes
    the recorded response, and is explicit rather than silently editing scores.
    """
    cases = {c.id: c for c in CASES}
    original_cases = {c["id"]: c for c in report["cases"]}
    for case_id, original in original_cases.items():
        current = public_case(cases[case_id])
        for key in ("id", "category", "text", "state", "history", "forbidden"):
            if json.dumps(original[key], ensure_ascii=False) != json.dumps(current[key], ensure_ascii=False):
                raise ValueError("Cannot regrade changed model inputs or safety labels")
    for row in report["samples"]:
        if not row.get("technical_success"):
            continue
        case = cases[row["case"]]
        attempt = row["attempts"][-1]
        session = fixture(case, RealtimeConfig())
        try:
            updated = grade(case, attempt["response"], attempt["route_text"], attempt["audio_bytes"],
                            consent=session.consent_context(), input_text=attempt.get("transcript"))
        finally:
            await session.close()
        if any(row[k] != value for k, value in updated.items()):
            row.setdefault("original_grading", {k: row[k] for k in updated})
        row.update(updated)
    report.setdefault("original_cases", report["cases"])
    report["cases"] = [public_case(cases[c["id"]]) for c in report["cases"]]
    report["grading_notes"] = ("Selector-equivalence fixes accept scope=all without redundant selection=all, "
        "accept scope=all when only one photo task exists, reject contradictory task IDs, "
        "and accept explicit R1 with an equivalent selector. Expert question paraphrases/translations "
        "are not penalized because the backend replaces that argument with the original user turn. "
        "Original labels and any changed grades are retained. No model inputs or responses changed.")
    report["summary"] = summarize(report["samples"])
    return report


def render(report):
    samples = report["samples"]
    stats = summarize(samples)
    lines = [f"# Realtime comparison — {report['mode']}", "",
        f"Started: {report['created_at']}. Finished: {report.get('finished_at', 'incomplete')}.", "",
        f"{len(report['cases'])} scenarios × {report['rounds']} rounds × {len(stats)} providers. "
        f"Pairing verified: {report.get('pairing_valid', False)}.", "",
        "Same production prompts, tool definitions, state, conversation history and input. "
        "Fresh connection for each sample; alternating provider order; no executed tools or field recordings.", "",
        "An action is correct only when the tool and meaningful arguments match an allowed outcome. "
        "Strict routing additionally requires no text/audio before the call. Extra routing text is "
        "suppressed by the app, so it is reported separately from choosing the right action.", "",
        "| Provider / model | Correct action | Strict routing | API completion | Median route | P95 route | Non-allow guard audits |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for provider, s in stats.items():
        n = s["samples"]
        pct = lambda k: f"{s[k]}/{n} ({s[k]/n:.1%})"
        ms = lambda k: f"{s[k]/1000:.2f}s" if s[k] is not None else "—"
        lines.append(f"| {provider} / `{s['model']}` | {pct('arguments_correct')} | {pct('passed')} | "
                     f"{pct('completed')} | {ms('route_median_ms')} | {ms('route_p95_ms')} | {s['guard_interceptions']} |")
    lines += ["", "Latency includes the route session update and generation, excluding connection setup, "
              "speech input, tool execution and spoken playback. Retried route attempts are included; retry "
              "connection setup is excluded. Fresh sessions do not measure warm-session phone latency.", "",
              "## Correct actions by category", "", "| Category | " + " | ".join(stats) + " |",
              "| --- | " + " | ".join("---:" for _ in stats) + " |"]
    for category in sorted({r["category"] for r in samples}):
        cells = []
        for provider in stats:
            rows = [r for r in samples if r["provider"] == provider and r["category"] == category]
            cells.append(f"{sum(r.get('arguments_correct', False) for r in rows)}/{len(rows)}")
        lines.append("| " + category + " | " + " | ".join(cells) + " |")
    if set(stats) == {"higgs", "openai"}:
        pairs = {}
        for row in samples:
            pairs.setdefault((row["round"], row["case"]), {})[row["provider"]] = row
        counts = {"both_correct": 0, "openai_only": 0, "higgs_only": 0, "both_incorrect": 0}
        for pair in pairs.values():
            if set(pair) != {"higgs", "openai"}:
                continue
            higgs, openai = (pair[p].get("arguments_correct", False) for p in ("higgs", "openai"))
            key = "both_correct" if higgs and openai else "openai_only" if openai else "higgs_only" if higgs else "both_incorrect"
            counts[key] += 1
        lines += ["", "Paired action outcomes: " + "; ".join(f"{key}={value}" for key, value in counts.items()) + "."]
    if any("speech" in s for s in stats.values()):
        lines += ["", "## Synthetic microphone transcription", "",
                  "Identical WAV audio from local Windows voices, streamed at real-time speed. "
                  "No microphone, noise or speaker echo. The model receives the audio and its actual "
                  "STT transcript; it does not receive the known script as a user message. Split VAD "
                  "transcripts are collected in audio order before routing the full fixture. "
                  "This does not measure production turn-coalescing or interruption timing.", "",
                  "| Provider | Exact normalized transcript | Token errors |", "| --- | ---: | ---: |"]
        for provider, s in stats.items():
            if "speech" in s:
                speech = s["speech"]
                lines.append(f"| {provider} | {speech['exact_normalized']}/{speech['samples']} | "
                             f"{speech['edit_errors']}/{speech['reference_tokens']} ({speech['token_error_rate']:.1%}) |")
        lines += ["", "Punctuation/case are ignored; Chinese characters and English words are tokens. "
                  "Digits versus spelled-out numbers still count as transcription differences. "
                  "Use action accuracy, not this small synthetic STT score alone, for product decisions."]
        lines += ["", "| Case / round | Script | " + " | ".join(stats) + " |",
                  "| --- | --- | " + " | ".join("---" for _ in stats) + " |"]
        pairs = {}
        for row in samples:
            pairs.setdefault((row["round"], row["case"]), {})[row["provider"]] = row
        cell = lambda value: str(value).replace("|", "\\|").replace("\n", " ")
        for (round_number, case_id), pair in sorted(pairs.items()):
            script = next(iter(pair.values()))["input"]
            transcripts = [pair[p]["attempts"][-1].get("transcript", pair[p].get("error_type", ""))
                           if p in pair else "missing" for p in stats]
            lines.append("| " + " | ".join(map(cell, [f"{case_id} / {round_number}", script, *transcripts])) + " |")
    lines += ["", "## Incorrect actions / technical failures", ""]
    failures = [r for r in samples if not r.get("arguments_correct")]
    if not failures:
        lines.append("None in this run.")
    for r in failures:
        calls = json.dumps(r.get("calls", []), ensure_ascii=False)
        lines.append(f"- **{r['provider']}, round {r['round']}, {r['case']}**: {r['input']} "
                     f"→ `{calls}`" + (f" (technical failure: {r.get('error_type')})" if not r.get("technical_success") else ""))
    lines += ["", "## Routing text/audio and protective checks", ""]
    for provider, s in stats.items():
        lines.append(f"- {provider}: {s['route_speech']} responses emitted routing text/audio; "
                     f"{s['forbidden_attempts']} samples attempted an explicitly forbidden action; "
                     f"{s['guard_interceptions']} triggered non-allow decisions in the existing shared guard audit; "
                     f"{s['schema_errors']} completed responses violated the single-call/schema requirement; "
                     f"{s['retries']} retries.")
    otherwise_correct = [r for r in samples if r.get("arguments_correct") and r["calls"][0]["tool"] != "ignore"
                         and any(g["decision"] != "allow" for g in r.get("guard", []))]
    if otherwise_correct:
        lines += ["", "Correct model actions that would still encounter a non-allow shared guard decision:", ""]
        for r in otherwise_correct:
            transcript = r["attempts"][-1].get("transcript", r["input"])
            lines.append(f"- {r['provider']}, {r['case']}, round {r['round']}: input `{transcript}` "
                         f"→ `{r['calls'][0]['tool']}`; guard `{json.dumps(r['guard'], ensure_ascii=False)}`.")
    lines += ["", "The guard is audited only; it does not rescue model scores or prove an end-to-end outcome. "
              "Some raw filler cases are filtered before reaching the router in the running app.", "",
              "## Limits", "",
              "These are hand-authored regression cases, with repeated identical intents; the trials "
              "are not independent real-world users and no population-level significance is claimed. "
              "Only the current provider configurations are compared. This run does not assess spoken "
              "answer quality, echo cancellation, long-dialogue memory, species recognition accuracy, "
              "or successful execution of investigations. BirdNET, BioCLIP, iNaturalist and GPT expert "
              "reasoning were not invoked. No production prompts were tuned using these results.", ""]
    if report.get("grading_notes"):
        lines += ["Scoring audit: " + report["grading_notes"], ""]
    return "\n".join(lines)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--regrade-json", type=Path, help="Optional explicit offline rescoring; preserves raw input JSON")
    args = parser.parse_args()
    report = json.loads(args.report.read_text(encoding="utf-8"))
    if args.regrade_json:
        import asyncio
        report = asyncio.run(regrade(report))
        args.regrade_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render(report), encoding="utf-8")
    print(args.output)
