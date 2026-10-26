"""Opt-in real Cloud/STT/speaker smoke for merged modes and native recordings.

Uses synthetic voice only. Creates and deletes one temporary speaker profile.
No physical microphone, loudspeaker echo or human identity claim is tested.
"""
import argparse
import asyncio
import json
from pathlib import Path
import time
import uuid

import numpy as np
from livekit_cloud_smoke import main as cloud_run, until, wav, ROOT
from speaker_client import speaker_request


def spoken(events, marker):
    return [e for e in events if e.get('_at',0)>marker and e.get('type')=='transcript' and e.get('role')=='assistant']


async def main(port, speaker_url):
    profile='feature-'+uuid.uuid4().hex[:8]
    async def scenario(port, session, ws, queued, events, state, audio, report):
        report.update(synthetic_voice=True, real_speaker_service=True, shadow_only=True)
        await ws.send(json.dumps({'type':'recording','enabled':True}))
        await until(lambda:state.get('recording_enabled'))
        await ws.send(json.dumps({'type':'enroll_start','profile':profile}))
        await until(lambda:any(e.get('type')=='enrollment' and e.get('status')=='capturing' for e in events))
        marker=time.monotonic()
        queued.extend(wav('enrollment-test.wav'))
        await until(lambda:any(e.get('type')=='enrollment' and e.get('status') in ('ok','error') for e in events),45)
        enrollment=next(e for e in events if e.get('type')=='enrollment' and e.get('status') in ('ok','error'))
        assert enrollment['status']=='ok', enrollment.get('error')
        assert 18 <= enrollment['duration_s'] <= 20.01
        await asyncio.sleep(1.3)
        assert not spoken(events,marker), 'Enrollment reading triggered assistant speech'
        assert not any(e.get('type') in ('capture','camera_prepare') or
                       (e.get('type')=='investigations' and e.get('items')) for e in events), 'Reading launched a task'
        report['checks'].append({'case':'Twenty-second native RTC enrollment succeeds and suppresses reading commands',
                                'duration_s':enrollment['duration_s'],'passed':True})

        marker=time.monotonic()
        queued.extend(wav('scout-check.wav'))
        await until(lambda:spoken(events,marker) and not state.get('speaking'),45)
        path=ROOT/'recordings'/'turns.jsonl'
        def rows():
            return [row for line in path.read_text(encoding='utf-8').splitlines()
                    if (row:=json.loads(line)).get('connection')==session] if path.exists() else []
        await until(lambda:any(row.get('routed_tool')=='speak' and profile in row.get('speaker_scores',{}) for row in rows()),10)
        row=next(row for row in rows() if row.get('routed_tool')=='speak' and profile in row.get('speaker_scores',{}))
        assert row['wav_filename'] and (path.parent/row['wav_filename']).exists()
        assert row['alignment']=='livekit_vad_native_voice_position' and row['assistant_audio']
        report['checks'].append({'case':'Native voice WAV, transcript, actual route and real shadow score share one turn',
                                'score':row['speaker_scores'][profile],'passed':True})

        await ws.send(json.dumps({'type':'recording','enabled':False}))
        await until(lambda:not state.get('recording_enabled'))
        files=set(path.parent.glob('*.wav'))
        marker=time.monotonic()
        queued.extend(wav('scout-barge.wav'))
        await until(lambda:any(any(word in e.get('text','').lower() for word in ('zero','no birds','0 birds'))
                               for e in spoken(events,marker)) and not state.get('speaking'),45)
        await asyncio.sleep(.3)
        assert set(path.parent.glob('*.wav'))==files, 'Disabling recording still created voice files'
        report['checks'].append({'case':'Recording off stops WAV saving while normal speech continues','passed':True})

        await ws.send(json.dumps({'type':'addressing_mode','value':'ambient'}))
        await until(lambda:state.get('addressing_mode')=='ambient')
        marker=time.monotonic(); before=audio['audible_frames']
        queued.extend(wav('ambient-question.wav'))
        await until(lambda:any(e.get('_at',0)>marker and e.get('type')=='silent_note' for e in events),35)
        assert not spoken(events,marker) and audio['audible_frames']==before, 'Silent card produced an answer or new audio'
        note=next(e for e in events if e.get('_at',0)>marker and e.get('type')=='silent_note')
        assert 'hawk' in note['question'].lower()
        report['checks'].append({'case':'Ambient uncertain question creates a silent card without audio','passed':True})
        marker=time.monotonic()
        await ws.send(json.dumps({'type':'answer_note','id':note['id']}))
        await until(lambda:spoken(events,marker) or any(e.get('_at',0)>marker and e.get('type')=='camera_prepare' for e in events),35)
        assert any(e.get('type')=='silent_note_removed' and e.get('id')==note['id'] for e in events)
        report['checks'].append({'case':'Answer button promotes the original card question to an explicit request','passed':True})
        await until(lambda:not state.get('speaking'))

        await ws.send(json.dumps({'type':'addressing_mode','value':'scout'}))
        await until(lambda:state.get('wake_required'))
        marker=time.monotonic()
        queued.extend(wav('bystander.wav'))
        await asyncio.sleep(5)
        assert not spoken(events,marker)
        assert not any(e.get('_at',0)>marker and e.get('type')=='silent_note' for e in events)
        report['checks'].append({'case':'Switching back to Scout restores silent ignoring of unaddressed chatter','passed':True})

    try:
        return await cloud_run(port,scenario=scenario,report_name='feature-smoke.json')
    finally:
        await asyncio.to_thread(speaker_request,speaker_url,'reset',np.empty(0),profile)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=8807)
    parser.add_argument('--speaker-url',default='http://127.0.0.1:8821')
    args=parser.parse_args()
    raise SystemExit(asyncio.run(main(args.port,args.speaker_url)))
