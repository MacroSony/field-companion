#!/usr/bin/env python3
"""Route-first feature checks and 18-turn probe; only writes docs/router-test.md.
Run with PYTHONDONTWRITEBYTECODE=1 and the probe venv Python.
"""
import argparse
import asyncio
import base64
import contextlib
import json
from pathlib import Path
import re
import statistics
import time
from datetime import datetime, timezone

from realtime_probe import connect, collect, tts, stream_pcm, silence
from ignore_probe import CASES, TOOLS as BASE_TOOLS, INSTRUCTIONS, execute, added_claim

REPORT = Path(__file__).resolve().parents[1] / 'docs/router-test.md'
MODEL = 'higgs-realtime-1.5-preview'
TOOLS = BASE_TOOLS + [{"type": "function", "name": "speak", "description": "The user addressed the assistant and needs a spoken answer; do not answer during routing.", "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"], "additionalProperties": False}}]
ROUTING = INSTRUCTIONS + '''
ROUTING PHASE ONLY: Select exactly ONE function call, with no text or audio.
For questions about today's recorded list/count call get_checklist. For other
addressed questions call speak(reason), NOT a direct answer. For chatter/self-talk
not addressed to you call ignore. For nature sightings call add_observation.
Classify only the latest user turn; earlier tool outputs are already handled.
Never give delayed acknowledgements of earlier logs. Ambiguous exclamations may
use ignore or speak. Do not invent a sighting from an identification question.
'''
ANSWER = '''Answer the latest addressed user question in ONE short sentence in the user's language.
Use the supplied tool output/checklist; do not invent observations. If identification
lacks evidence, ask for a description. Do not acknowledge earlier logging or chatter.
This is the spoken-answer phase, not routing; do not call tools.'''


def session(audio=True, **overrides):
    s = {"output_modalities": ["audio" if audio else "text"], "instructions": ROUTING,
         "tools": TOOLS, "tool_choice": "auto"}
    if audio:
        s['audio'] = {"input": {"turn_detection": {"type": "server_vad", "create_response": False, "interrupt_response": True}, "transcription": {"model": "higgs-stt-3.1"}}}
    s.update(overrides)
    return s


def record(label):
    return {"label": label, "events": [], "errors": [], "echoes": [], "text": "", "audio_bytes": 0, "calls": [], "statuses": [], "marks": {}, "response_created": []}


def observer(row):
    async def on(e, t):
        ty = e['type']
        if not ty.endswith('.delta'):
            row['events'].append([round(t, 4), ty])
        row['marks'].setdefault(ty, round(t, 6))
        if ty in ('session.created', 'session.updated'):
            row['echoes'].append({"event": ty, "session": e.get('session')})
        elif ty == 'error':
            row['errors'].append(e.get('error'))
        elif ty in ('response.output_text.delta', 'response.output_audio_transcript.delta'):
            row['text'] += e.get('delta', '')
        elif ty == 'response.output_audio.delta':
            row['audio_bytes'] += len(base64.b64decode(e['delta']))
        elif ty == 'conversation.item.input_audio_transcription.completed':
            row['heard'] = e.get('transcript')
            row['transcription_item_id'] = e.get('item_id')
        elif ty == 'response.created':
            row['response_created'].append(e.get('response'))
        elif ty == 'response.done':
            r = e.get('response', {})
            row['statuses'].append(r.get('status'))
            row['calls'].extend(i for i in r.get('output', []) if i.get('type') == 'function_call')
            if r.get('status_details'):
                row.setdefault('status_details', []).append(r['status_details'])
    return on


async def gather(ws, row, t0, until=('response.done', 'error'), timeout=20):
    # collect's timeout is per recv; outer timeout is an absolute operation bound.
    try:
        return await asyncio.wait_for(collect(ws, t0=t0, until=until, timeout=timeout, on=observer(row)), timeout + .2)
    except asyncio.TimeoutError:
        row['errors'].append('absolute collection timeout')
        return {'timeout': True}


async def drain(ws, row, t0, seconds=.4):
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        try:
            e = json.loads(await asyncio.wait_for(ws.recv(), end - time.perf_counter()))
        except asyncio.TimeoutError:
            break
        await observer(row)(e, time.perf_counter() - t0)


async def opening(s, row):
    t0 = time.perf_counter()
    ws = await connect(s, model=MODEL)
    await gather(ws, row, t0, until=('session.created', 'session.updated', 'error'), timeout=12)
    await drain(ws, row, t0, .4)
    return ws


async def user_text(ws, text):
    await ws.send(json.dumps({"type": "conversation.item.create", "item": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}}))


async def create(ws, row, response):
    row['request'] = {"type": "response.create", "response": response}
    await ws.send(json.dumps(row['request']))


def delta(row, end, start):
    m = row['marks']
    return round((m[end] - m[start]) * 1000, 1) if end in m and start in m else None


def save(data):
    out = ['# Route first, speak second — Higgs Realtime', '', f"Started UTC: {data['started']}. Updated incrementally.", '',
           'Model: `higgs-realtime-1.5-preview`. Same 18 utterances/labels and fake checklist executor as `ignore_probe.py`; no labels sent to the model. Synthetic higgs-tts-3 PCM, 24 kHz, real-time chunks; server_vad + higgs-stt-3.1. No PCM or keys saved.', '',
           'Feature tests retain exact request, session echo, response metadata, event times, and errors below. A missing session.updated is not treated as rejection when session.created echoes settings. Audio no-auto observation continues 2 seconds after transcription (plus streaming completion).', '',
           'Routing latency = complete function arguments minus response.create send; transcript-ready and first spoken audio are measured from speech_stopped receipt. Text latency starts at submission. Generated routing text/audio is counted even though a production client would mute it. False-act/ignore definitions match ignore-test; preambles means any generated speech on a sighting turn, with before-call timing separately retained. Errors are not successful silence. One sample is not a reliability estimate.', '',
           '## Feature support', '']
    out += data.get('conclusions', ['Feature checks in progress; see evidence below.'])
    out += ['', '## Comparison', '', '| Configuration | Turns | False acts / chatter | False ignores / log+question | Sighting speech / sightings | Errors |', '|---|---|---|---|---|---|',
           '| Earlier preview audio (ignore-test) | 18 | 3/6 | 0/10 | 3/6 | 0 |']
    for run in data.get('runs', []):
        rows = run['rows']
        fa = sum(r['expected'] == 'ignore' and r['actual'] not in ('ignored', 'error') for r in rows)
        fi = sum(r['expected'] in ('log', 'answer') and any(c['name'] == 'ignore' for c in r['calls']) for r in rows)
        pre = sum(r['expected'] == 'log' and bool(r['audio_bytes'] or r['text']) for r in rows)
        err = sum(r['actual'] == 'error' for r in rows)
        chatter = sum(r['expected'] == 'ignore' for r in rows)
        addressed = sum(r['expected'] in ('log', 'answer') for r in rows)
        sightings = sum(r['expected'] == 'log' for r in rows)
        out.append(f"| Router {run['mode']} | {len(rows)}/18 | {fa}/{chatter} | {fi}/{addressed} | {pre}/{sightings} | {err} |")
    for run_index, run in enumerate(data.get('runs', []), 1):
        rows = run['rows']
        out += ['', f"### Run {run_index}: router {run['mode']}: {run['status']}", '', '| # | Utterance | Label → actual | Tools | STT ready ms | Route ms | First spoken audio ms |', '|---|---|---|---|---|---|---|']
        for i, r in enumerate(rows, 1):
            out.append('| ' + ' | '.join(str(x).replace('|', '\\|') for x in [i, r['utterance'], r['expected'] + ' → ' + r['actual'], ', '.join(c['name'] for c in r['calls']) + (' DING' if r.get('ding') else ''), r.get('transcript_ready_ms'), r.get('routing_ms'), r.get('spoken_audio_ms')]) + ' |')
    out += ['', '## Latency (complete sessions only)', '', '| Mode | Metric | n | Median ms | Range ms |', '|---|---|---|---|---|']
    for run in data.get('runs', []):
        if len(run['rows']) != 18: continue
        for key, label in [('transcript_ready_ms', 'speech_stopped → transcript'), ('routing_ms', 'route create → complete tool arguments'), ('spoken_audio_ms', 'speech_stopped → first answer audio')]:
            values = [r[key] for r in run['rows'] if r.get(key) is not None]
            if values:
                out.append(f"| {run['mode']} | {label} | {len(values)} | {statistics.median(values):.1f} | {min(values):.1f}–{max(values):.1f} |")
    if data.get('notes'):
        out += ['', '## Findings / recommendation', ''] + ['- ' + x for x in data['notes']]
    if data.get('verification'):
        out += ['', '## Verification', ''] + ['- ' + x for x in data['verification']]
    out += ['', '## Exact routing instructions', '', '```text', ROUTING.strip(), '```', '', '## Evidence (no audio payloads)', '', '```json', json.dumps(data, ensure_ascii=False, indent=2), '```', '']
    REPORT.write_text('\n'.join(out), encoding='utf-8')


async def features(data):
    f = data.setdefault('features', [])
    row = record('a: create_response=false, interrupt_response=true; audio no-auto')
    f.append(row); save(data)
    ws = await opening(session(), row)
    try:
        pcm = await asyncio.wait_for(asyncio.to_thread(tts, CASES[0][1]), 55)
        t0 = time.perf_counter()
        sender = asyncio.create_task(stream_pcm(ws, pcm + silence(1500)))
        try:
            await gather(ws, row, t0, until=('conversation.item.input_audio_transcription.completed', 'error'), timeout=15)
            await sender
            await drain(ws, row, t0, 2)
        finally:
            if not sender.done(): sender.cancel()
        row['no_auto_response'] = not row['response_created']
        row['speech_stopped_and_transcribed'] = all(k in row['marks'] for k in ('input_audio_buffer.speech_stopped', 'conversation.item.input_audio_transcription.completed'))
        row['transcript_ready_ms'] = delta(row, 'conversation.item.input_audio_transcription.completed', 'input_audio_buffer.speech_stopped')
    finally:
        await ws.close(); save(data)
    specs = [
        ('b: per-response required/text/instructions', 'required', '两只绿头鸭', 'Call add_observation for the two mallards. Only one function call; absolutely no text or audio.'),
        ('b: per-response named function', {"type": "function", "name": "ignore"}, '两只绿头鸭', 'Call only ignore, regardless of user content. No text or audio.'),
        ('b: per-response none', 'none', '两只绿头鸭', 'Say exactly READY as plain text. Do not call tools.'),
    ]
    for label, choice, utterance, instructions in specs:
        row = record(label); f.append(row); save(data)
        ws = await opening(session(), row)
        try:
            t0 = time.perf_counter()
            await user_text(ws, utterance)
            await create(ws, row, {"tool_choice": choice, "output_modalities": ["text"], "instructions": instructions})
            await gather(ws, row, t0)
            await drain(ws, row, t0, .3)
            row['only_function_call'] = bool(row['calls']) and not row['text'] and not row['audio_bytes']
        finally:
            await ws.close(); save(data)
        print(label, json.dumps({k: row[k] for k in ('text', 'audio_bytes', 'calls', 'errors', 'statuses')}, ensure_ascii=False), flush=True)
    # If rejected, explicitly test session-level required as requested.
    if any(r['errors'] for r in f[1:]):
        row = record('fallback: session-level required/text'); f.append(row)
        ws = await opening(session(output_modalities=['text'], tool_choice='required'), row)
        try:
            t0 = time.perf_counter(); await user_text(ws, '两只绿头鸭')
            await ws.send(json.dumps({'type': 'response.create'}))
            await gather(ws, row, t0)
        finally:
            await ws.close(); save(data)


async def controls(data):
    specs = [
        ('isolated: response tool_choice required', session(audio=False, instructions='Reply exactly READY as plain text; do not use tools.'), {'tool_choice': 'required'}),
        ('isolated: response named ignore', session(audio=False), {'tool_choice': {'type': 'function', 'name': 'ignore'}}),
        ('isolated: response tool_choice none', session(audio=False), {'tool_choice': 'none'}),
        ('isolated: response instructions', session(audio=False, instructions='Reply exactly SESSION.'), {'instructions': 'Reply exactly OVERRIDE.'}),
        ('isolated: response output_modalities text', session(instructions='Reply exactly READY.'), {'output_modalities': ['text']}),
    ]
    for label, settings, overrides in specs:
        row = record(label); data['features'].append(row); save(data)
        ws = await opening(settings, row)
        try:
            t0 = time.perf_counter(); await user_text(ws, '两只绿头鸭')
            await create(ws, row, overrides); await gather(ws, row, t0)
            await drain(ws, row, t0, .3)
        finally:
            await ws.close(); save(data)
        print(label, json.dumps({k: row[k] for k in ('text', 'audio_bytes', 'calls', 'errors', 'statuses')}, ensure_ascii=False), flush=True)


async def diagnostics(data):
    specs = [
        ('diagnostic: audio input, per-response required/text', session(), '两只绿头鸭', {'tool_choice': 'required', 'output_modalities': ['text'], 'instructions': ROUTING}),
        ('diagnostic: audio input, per-response named speak', session(), '两只绿头鸭', {'tool_choice': {'type': 'function', 'name': 'speak'}, 'output_modalities': ['text'], 'instructions': 'Call speak with reason test. Do not call add_observation. No text or audio.'}),
        ('diagnostic: audio input, per-response none', session(), '两只绿头鸭', {'tool_choice': 'none', 'output_modalities': ['text'], 'instructions': 'Reply exactly READY. Do not call any tool.'}),
        ('fallback: session required/text with audio input', session(output_modalities=['text'], tool_choice='required'), '两只绿头鸭', None),
        ('diagnostic: text session per-response required', session(audio=False), '两只绿头鸭', {'tool_choice': 'required', 'output_modalities': ['text'], 'instructions': ROUTING}),
    ]
    for label, settings, utterance, overrides in specs:
        row = record(label); data['features'].append(row); save(data)
        ws = await opening(settings, row); sender = None
        try:
            pcm = await asyncio.wait_for(asyncio.to_thread(tts, utterance), 45) if 'audio' in settings else None
            t0 = time.perf_counter()
            if pcm is not None:
                sender = asyncio.create_task(stream_pcm(ws, pcm + silence(1500)))
                await gather(ws, row, t0, until=('conversation.item.input_audio_transcription.completed', 'error'), timeout=15)
            else:
                await user_text(ws, utterance)
            await create(ws, row, overrides or {})
            await gather(ws, row, t0)
            if sender: await sender
            await drain(ws, row, t0, .4)
            row['only_function_call'] = bool(row['calls']) and not row['text'] and not row['audio_bytes']
        finally:
            if sender and not sender.done(): sender.cancel()
            await ws.close(); save(data)
        print(label, json.dumps({k: row[k] for k in ('text', 'audio_bytes', 'calls', 'errors', 'statuses')}, ensure_ascii=False), flush=True)


async def router(data, mode, deadline):
    fallback = mode.startswith('fallback-')
    audio = mode.endswith('audio')
    run = {'mode': mode, 'status': 'Running', 'rows': []}; data.setdefault('runs', []).append(run); save(data)
    setup = record('router setup'); run['setup'] = setup
    settings = session(audio=audio)
    if fallback: settings.update(output_modalities=['text'], tool_choice='required')
    ws = await opening(settings, setup)
    checklist = []
    try:
        for expected, utterance in CASES:
            if time.monotonic() > deadline - 35:
                run['status'] = 'Wall-time budget stop'; break
            row = record(utterance); row.update(utterance=utterance, expected=expected, actual='error', ding=0)
            pcm = await asyncio.wait_for(asyncio.to_thread(tts, utterance), 55) if audio else None
            t0 = time.perf_counter(); sender = None
            try:
                if fallback:
                    row['route_session_update'] = {'type': 'session.update', 'session': {'output_modalities': ['text'], 'tool_choice': 'required', 'instructions': ROUTING}}
                    await ws.send(json.dumps(row['route_session_update']))
                if audio:
                    row['input_audio_s'] = len(pcm) / 48000
                    sender = asyncio.create_task(stream_pcm(ws, pcm + silence(1500)))
                    await gather(ws, row, t0, until=('conversation.item.input_audio_transcription.completed', 'error'), timeout=18)
                    if 'heard' not in row: raise RuntimeError('no input transcription')
                    row['transcript_ready_ms'] = delta(row, 'conversation.item.input_audio_transcription.completed', 'input_audio_buffer.speech_stopped')
                    if row['response_created']: row['errors'].append('automatic response before routing')
                else:
                    await user_text(ws, utterance)
                row['route_sent_s'] = time.perf_counter() - t0
                await create(ws, row, {} if fallback else {'tool_choice': 'required', 'output_modalities': ['text'], 'instructions': ROUTING})
                await gather(ws, row, t0)
                row['route_text'] = row['text']; row['route_audio_bytes'] = row['audio_bytes']
                call_time = row['marks'].get('response.function_call_arguments.done')
                row['routing_ms'] = round((call_time - row['route_sent_s']) * 1000, 1) if call_time is not None else None
                output_time = min([row['marks'][x] for x in ('response.output_text.delta', 'response.output_audio.delta', 'response.output_audio_transcript.delta') if x in row['marks']], default=float('inf'))
                row['output_before_call'] = output_time < row['marks'].get('response.function_call_arguments.done', float('inf'))
                routing_calls = list(row['calls'])
                for call in routing_calls:
                    args = json.loads(call.get('arguments') or '{}')
                    output = {'ready_to_answer': True, 'reason': args.get('reason')} if call['name'] == 'speak' else execute(call['name'], args, checklist)
                    if call['name'] == 'add_observation': row['ding'] += 1; print('DING', flush=True)
                    await ws.send(json.dumps({'type': 'conversation.item.create', 'item': {'type': 'function_call_output', 'call_id': call['call_id'], 'output': json.dumps(output, ensure_ascii=False)}}))
                if any(c['name'] in ('speak', 'get_checklist') for c in routing_calls):
                    row['answer_request'] = {'tool_choice': 'none', 'output_modalities': ['audio' if audio else 'text'], 'instructions': ANSWER}
                    if fallback:
                        row['answer_session_update'] = {'type': 'session.update', 'session': row['answer_request']}
                        await ws.send(json.dumps(row['answer_session_update']))
                    await ws.send(json.dumps({'type': 'response.create', 'response': {} if fallback else row['answer_request']}))
                    await gather(ws, row, t0)
                if sender: await sender
                await drain(ws, row, t0, .6)
                row['spoken_audio_ms'] = delta(row, 'response.output_audio.delta', 'input_audio_buffer.speech_stopped')
                names = [c['name'] for c in row['calls']]
                has_output = bool(row['text'] or row['audio_bytes'])
                if row['errors'] or 'failed' in row['statuses']: row['actual'] = 'error'
                elif names and set(names) == {'ignore'} and not has_output: row['actual'] = 'ignored'
                elif 'ignore' in names and has_output: row['actual'] = 'spoke-then-ignored' if row['output_before_call'] else 'ignored-then-spoke'
                elif 'add_observation' in names: row['actual'] = 'logged'
                elif has_output: row['actual'] = 'answered'
                else: row['actual'] = 'no-action'
                row['added_without_tool'] = added_claim(row)
            except Exception as exc:
                row['errors'].append(f'{type(exc).__name__}: {exc}')
            finally:
                if sender and not sender.done():
                    sender.cancel()
                    with contextlib.suppress(asyncio.CancelledError): await sender
                row['checklist_after'] = json.loads(json.dumps(checklist, ensure_ascii=False))
                run['rows'].append(row); save(data)
                print(mode, len(run['rows']), row['actual'], row.get('routing_ms'), row.get('spoken_audio_ms'), row['errors'], flush=True)
            if row['errors'] and not all(isinstance(e, dict) and e.get('code') == 'upstream_error' for e in row['errors']):
                run['status'] = 'Stopped on protocol/transport error; evidence retained'; break
        else:
            run['status'] = 'Complete 18-turn sequence' + (' (includes server-failed turns)' if any(r['errors'] for r in run['rows']) else '')
    finally:
        await ws.close(); save(data)


async def main():
    p = argparse.ArgumentParser(); p.add_argument('phase', choices=['features', 'diagnostics', 'controls', 'audio', 'text', 'fallback-audio', 'fallback-text']); p.add_argument('--budget-seconds', type=int, default=400); args = p.parse_args()
    if args.phase == 'features':
        data = {'started': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'features': [], 'runs': [], 'notes': []}
    else:
        data = json.loads(re.search(r'```json\n(.*?)\n```', REPORT.read_text(), re.S)[1])
    try:
        operation = features(data) if args.phase == 'features' else diagnostics(data) if args.phase == 'diagnostics' else controls(data) if args.phase == 'controls' else router(data, args.phase, time.monotonic() + args.budget_seconds)
        await asyncio.wait_for(operation, args.budget_seconds)
    except Exception as exc:
        data.setdefault('notes', []).append(f'{args.phase} terminated: {type(exc).__name__}: {exc}')
    finally:
        save(data)


if __name__ == '__main__':
    asyncio.run(main())
