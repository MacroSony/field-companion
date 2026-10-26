"""Opt-in real LiveKit Cloud/GPT audio test with a synthetic microphone.

Requires the running local --enable-livekit application and saved Cloud keys.
Makes billed API calls; creates only a temporary room and isolated app session.
Does not exercise browser AEC, speaker echo, a physical phone or BirdNET.
"""
import argparse
import asyncio
import json
from pathlib import Path
import time
import uuid
import wave

import httpx
import numpy as np
from livekit import rtc
import websockets

ROOT=Path(__file__).resolve().parents[1]


async def until(predicate,seconds=35):
    async with asyncio.timeout(seconds):
        while not predicate():
            await asyncio.sleep(.05)


def wav(name):
    with wave.open(str(ROOT/'out/audio-lab'/name),'rb') as source:
        if source.getframerate()!=48000 or source.getnchannels()!=1 or source.getsampwidth()!=2:
            raise ValueError('Generate the 48 kHz mono PCM16 audio lab fixtures first')
        return source.readframes(source.getnframes())


async def main(port, *, scenario=None, report_name='cloud-smoke.json'):
    session_id='livekit-cloud-smoke-'+uuid.uuid4().hex[:12]
    report={'passed':False,'session':session_id,'physical_phone':False,'browser_aec_test':False,
            'acoustic_echo_test':False,'cloud_transport_test':True,'checks':[]}
    events=[];tasks=[];audio_streams=[];source=None;room=rtc.Room()
    queued=bytearray();audio={'frames':0,'audible_frames':0,'audible_at':[]};state={}
    ws=None

    async def read_control():
        async for raw in ws:
            if not isinstance(raw,str):
                raise AssertionError('Duplicate WebSocket speech PCM')
            event=json.loads(raw);event['_at']=time.monotonic();events.append(event)
            if event.get('type')=='state':
                state.update(event)

    async def read_audio(track):
        stream=rtc.AudioStream(track,sample_rate=24000,num_channels=1)
        audio_streams.append(stream)
        try:
            async for packet in stream:
                audio['frames']+=1
                values=np.frombuffer(packet.frame.data,dtype='<i2').astype(np.int32)
                if np.max(np.abs(values),initial=0)>300:
                    audio['audible_frames']+=1
                    audio['audible_at'].append(time.monotonic())
        finally:
            await stream.aclose()

    @room.on('track_subscribed')
    def subscribed(track,publication,participant):
        if track.kind==rtc.TrackKind.KIND_AUDIO:
            tasks.append(asyncio.create_task(read_audio(track)))

    async def microphone():
        while True:
            started=time.monotonic()
            data=bytes(queued[:1920]);del queued[:1920]
            await source.capture_frame(rtc.AudioFrame(data.ljust(1920,b'\0'),
                sample_rate=48000,num_channels=1,samples_per_channel=960))
            await asyncio.sleep(max(0,.02-(time.monotonic()-started)))

    try:
        ws=await websockets.connect(f'ws://127.0.0.1:{port}/ws?role=mic&transport=livekit&session={session_id}')
        tasks.append(asyncio.create_task(read_control()))
        await ws.send(json.dumps({'type':'hello','lang':'en','playback_protocol':0}))
        await until(lambda:any(e.get('type')=='livekit_ready' for e in events))
        capability=next(e['token'] for e in events if e.get('type')=='livekit_ready')
        async with httpx.AsyncClient(timeout=25) as client:
            response=await client.post(f'http://127.0.0.1:{port}/api/livekit/connect',
                params={'session':session_id},headers={'X-Field-RTC':capability})
            response.raise_for_status();connection=response.json()
        await asyncio.wait_for(room.connect(connection['url'],connection['token']),30)
        source=rtc.AudioSource(48000,1,queue_size_ms=100)
        track=rtc.LocalAudioTrack.create_audio_track('synthetic microphone',source)
        await room.local_participant.publish_track(track,rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))
        tasks.append(asyncio.create_task(microphone()))
        await until(lambda:state.get('connected'),45)
        report['checks'].append({'case':'Application signaling and both participants connect through LiveKit Cloud','passed':True})
        await asyncio.sleep(4)  # Honor the SDK's initial AEC warm-up window.
        if scenario:
            await scenario(port, session_id, ws, queued, events, state, audio, report)
            report['passed'] = True
            return 0
        queued.extend(wav('scout-check.wav'))
        await until(lambda:any(e.get('type')=='transcript' and e.get('role')=='user' and 'hear' in e.get('text','').lower() for e in events),35)
        await until(lambda:any(e.get('type')=='transcript' and e.get('role')=='assistant' for e in events) and audio['audible_frames']>0,45)
        await until(lambda:not state.get('speaking'))
        report['checks'].append({'case':'Synthetic spoken question is transcribed and answered over real Cloud RTP audio','passed':True})

        marker=time.monotonic();previous=audio['audible_frames']
        await ws.send(json.dumps({'type':'text','text':'Please explain in detail how I can use the camera, bird listening, and checklist features.'}))
        await until(lambda:state.get('speaking') and audio['audible_frames']>previous,40)
        await asyncio.sleep(.5)
        chatter_at=time.monotonic()
        queued.extend(wav('bystander.wav'))
        await asyncio.sleep(5)
        assert not any(e.get('_at',0)>chatter_at and e.get('role')=='user' for e in events), 'Bystander speech entered accepted dialogue'
        assert state.get('speaking'), 'Bystander speech interrupted the long answer'
        report['checks'].append({'case':'Unaddressed speech during an answer does not enter dialogue or interrupt playout','passed':True})
        report['barge_started_while_speaking']=bool(state.get('speaking'))
        report['barge_started_at']=time.monotonic()
        queued.extend(wav('scout-barge.wav'))
        await until(lambda:any(e.get('_at',0)>marker and e.get('role')=='user' and 'checklist' in e.get('text','').lower() for e in events),35)
        await until(lambda:any(e.get('_at',0)>marker and e.get('role')=='assistant' and ('zero' in e.get('text','').lower() or 'no ' in e.get('text','').lower() or '0' in e.get('text','')) for e in events),45)
        report['checks'].append({'case':'Scout-addressed follow-up during a reply interrupts and reaches checklist routing and a new answer','passed':True})
        report['passed']=True
    except Exception as error:
        report['error_type']=type(error).__name__
        if isinstance(error, AssertionError):
            report['assertion'] = str(error)
        report['recent_controls']=[{'type':e.get('type'),'role':e.get('role'),
            'connected':e.get('connected'),'speaking':e.get('speaking')} for e in events[-12:]]
    finally:
        report['received_audio_frames']=audio['frames']
        report['audible_audio_frames']=audio['audible_frames']
        report['captions']=[{'role':e['role'],'text':e.get('text','')} for e in events if e.get('type')=='transcript']
        if ws:
            await ws.close()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        for stream in audio_streams:
            await stream.aclose()
        if source:
            await source.aclose()
        await room.disconnect()
        folder=ROOT/'out/livekit';folder.mkdir(exist_ok=True,parents=True)
        (folder/report_name).write_text(json.dumps(report,indent=2),encoding='utf-8')
        print(json.dumps({key:value for key,value in report.items() if key!='captions'}))
    return 0 if report['passed'] else 1


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=8805)
    arguments=parser.parse_args()
    raise SystemExit(asyncio.run(main(arguments.port)))
