"""Opt-in billed replay through real STT, routing, photo HTTP, BioCLIP and speech.

Synthetic RTC microphone/phone and a local JPEG replace physical devices. The
classifier, species lookup and both GPT connections are real. This checks the
workflow and readout of the returned evidence, not the photo's ground truth.
"""
import argparse
import asyncio
import json
from pathlib import Path
import time

import httpx
from livekit_cloud_smoke import main as cloud_run, until, wav, ROOT
from playout_evidence import received_speech_frames


def captions(events, marker):
    return [e.get('text','') for e in events if e.get('_at',0) > marker
            and e.get('type') == 'transcript' and e.get('role') == 'assistant']


async def verify_result_audio(events, audio, marker, expected):
    caption = next(e for e in events if e.get('_at',0)>marker and e.get('role')=='assistant'
                   and expected.lower() in e.get('text','').lower())
    await asyncio.sleep(.3)
    frames = received_speech_frames(events, audio['audible_at'], caption['_at'])
    assert frames > 0, 'Result text existed but its own speech window received no non-silent audio'
    return frames


async def scenario(photo, port, session, ws, queued, events, state, audio, report, *, invite=False):
    report.update(real_bioclip=True, real_taxonomy=True, synthetic_phone=True)
    marker = time.monotonic()
    queued.extend(wav('scout-invitation.wav' if invite else 'scout-greeting.wav'))
    await until(lambda:captions(events,marker) and not state.get('speaking'),45)
    assert all(text.strip().lower() != 'ignore' for text in captions(events,marker))
    report['checks'].append({'case':'Spoken Scout greeting receives a human-facing answer','passed':True})

    marker = time.monotonic()
    invited = bool(state.get('reply_without_name'))
    if invite:
        assert invited, 'Explicit request for a question did not open a reply window'
    queued.extend(wav('answer-animal.wav' if invited else 'scout-animal.wav'))
    await until(lambda:any(e.get('_at',0)>marker and e.get('type')=='camera_prepare' for e in events),45)
    prepare = next(e for e in events if e.get('_at',0)>marker and e.get('type')=='camera_prepare')
    observation = next(e for e in reversed(events) if e.get('type')=='observation' and e.get('id')==prepare['obs'])
    assert observation['group'] == 'Any', f'Generic animal was narrowed to {observation["group"]}'
    assert not any(e.get('type')=='capture' for e in events)
    report['checks'].append({'case':'Generic animal opens an unrestricted preview without taking a photo',
                            'name_free_reply':invited,'passed':True})

    marker = time.monotonic()
    await ws.send(json.dumps({'type':'camera_ready','request_id':prepare['request_id']}))
    await until(lambda:any('camera ready' in text.lower() for text in captions(events,marker)),25)
    queued.extend(wav('camera-start.wav'))
    await until(lambda:any(e.get('type')=='capture' for e in events),35)
    capture = next(e for e in events if e.get('type')=='capture')
    report['checks'].append({'case':'Spoken Start without Scout authorizes one photo after readiness','passed':True})

    marker = time.monotonic()
    async with httpx.AsyncClient(timeout=90) as client:
        response = await client.post(f'http://127.0.0.1:{port}/api/frame',
            params={'session':session,'obs':capture['obs'],'capture':capture['request_id']},content=photo.read_bytes())
        response.raise_for_status()
    candidates = response.json()['detection']['candidates']
    assert candidates, 'Real classifier returned no candidates'
    expected = candidates[0].get('en') or candidates[0]['sci']
    assert expected != candidates[0]['sci'], 'Species lookup did not supply a common name'
    await until(lambda:any(expected.lower() in text.lower() for text in captions(events,marker)),50)
    assert all(text.strip().lower() != 'ignore' for text in captions(events,marker)), 'Photo completion spoke a routing label'
    result_frames = await verify_result_audio(events,audio,marker,expected)
    await until(lambda:not state.get('speaking'))
    report['checks'].append({'case':'Successful photo automatically speaks the returned common name without another user turn',
                            'returned_name':expected,'result_audio_frames':result_frames,'passed':True})

    marker = time.monotonic()
    queued.extend(wav('scout-photo-status.wav'))
    await until(lambda:any(any(word in text.lower() for word in ('complete','ready','finished','done'))
                          for text in captions(events,marker)),45)
    await until(lambda:not state.get('speaking'))
    assert sum(e.get('type')=='capture' for e in events) == 1
    report['checks'].append({'case':'Spoken progress question reports completion without restarting capture','passed':True})

    marker = time.monotonic()
    queued.extend(wav('scout-photo-result.wav'))
    await until(lambda:any(expected.lower() in text.lower() for text in captions(events,marker)),45)
    result_frames = await verify_result_audio(events,audio,marker,expected)
    assert sum(e.get('type')=='capture' for e in events) == 1
    report['checks'].append({'case':'Spoken show-me-the-result reads existing evidence over RTP audio',
                            'result_audio_frames':result_frames,'passed':True})


async def main(port, photo, repeats, *, invite=False, report_prefix='camera-replay'):
    reports = []
    for index in range(repeats):
        name = f'{report_prefix}-{index+1}.json'
        async def run(*args):
            await scenario(photo, *args, invite=invite)
        await cloud_run(port,scenario=run,report_name=name)
        reports.append(json.loads((ROOT/'out/livekit'/name).read_text(encoding='utf-8')))
    summary = {'passed':all(row['passed'] for row in reports),'runs':len(reports),
               'passed_runs':sum(row['passed'] for row in reports),
               'checks':sum(len(row['checks']) for row in reports),
               'physical_phone':False,'acoustic_echo_test':False,'reports':reports}
    (ROOT/'out/livekit'/f'{report_prefix}.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
    print(json.dumps({key:value for key,value in summary.items() if key!='reports'}))
    return 0 if summary['passed'] else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=8805)
    parser.add_argument('--photo',type=Path,required=True)
    parser.add_argument('--repeats',type=int,choices=range(1,6),default=3)
    parser.add_argument('--invite',action='store_true',help='Exercise a spoken question followed by a name-free identification request')
    parser.add_argument('--report-prefix',choices=['camera-replay','camera-replay-invited'],default='camera-replay')
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.port,args.photo,args.repeats,invite=args.invite,report_prefix=args.report_prefix)))
