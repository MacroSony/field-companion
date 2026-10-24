"""Summarize clean trials, preserving upstream failures and wrong-tool routes."""
import json
from pathlib import Path
import statistics
import sys

D = Path('/data/build/tmp/overnight/E')
history = '--history' in sys.argv
suffix = '-history' if history else ''
rows = [json.loads(line) for line in (D / ('trials-history-polluted.jsonl' if history else 'trials.jsonl')).read_text().splitlines()]
summary = []
for flag in (0, 1):
    events = []
    for line in (D / (f'server-history-{flag}.log' if history else f'server-{flag}.log')).read_text().splitlines():
        try:
            e = json.loads(line)
            if 'perf' in e:
                events.append(e)
        except ValueError:
            pass
    trials = [r for r in rows if r['flag'] == flag]
    for ix, row in enumerate(trials):
        end = trials[ix + 1]['submit_perf'] if ix + 1 < len(trials) else float('inf')
        relevant = [e for e in events if row['submit_perf'] <= e['perf'] < end]
        audio = next((e for e in relevant if e['event'] == 'client_first_audio'), None)
        route = next((e for e in relevant if e['event'] == 'route_done'), None)
        row['actual_tools'] = route['tools'] if route else []
        row['route_first_audio_s'] = audio['perf'] - route['perf'] if audio and route else None
        row['fallback'] = any(e['event'] == 'template_fallback' for e in relevant)
    for case, expected in [('sounds', 'recent_sounds'), ('checklist', 'get_checklist')]:
        subset = [r for r in trials if r['case'] == case]
        good = [r for r in subset if r['route_first_audio_s'] is not None and r['actual_tools'] == [expected]]
        metric = lambda key: round(statistics.median(r[key] for r in good), 4) if good else None
        summary.append(dict(flag=flag, case=case, repetitions=len(set(r['rep'] for r in subset)), attempts=len(subset), correct_tool_audio_successes=len(good), no_audio=sum(r['text_submit_first_client_s'] is None for r in subset), wrong_tool=sum(bool(r['actual_tools']) and r['actual_tools'] != [expected] for r in subset), route_first_audio_median_s=metric('route_first_audio_s'), text_submit_first_audio_median_s=metric('text_submit_first_client_s'), route_first_audio_values_s=[round(r['route_first_audio_s'], 4) for r in good], fallbacks=sum(r['fallback'] for r in subset)))
(D / ('summary' + suffix + '.json')).write_text(json.dumps(summary, indent=2))
(D / ('trials-enriched' + suffix + '.json')).write_text(json.dumps(rows, indent=2))
print('| Flag | Question | Reps | Correct-tool audio / attempts | Route done → client audio median | Text submit → client audio median |')
print('|---|---|---:|---:|---:|---:|')
for r in summary:
    print(f"| {r['flag']} | {r['case']} | {r['repetitions']} | {r['correct_tool_audio_successes']}/{r['attempts']} | {r['route_first_audio_median_s']} s | {r['text_submit_first_audio_median_s']} s |")
