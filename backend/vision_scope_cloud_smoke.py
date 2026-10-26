"""Opt-in billed real RTC plant request -> photo -> conflict clarification.

Uses saved local credentials and synthetic microphone audio; no physical-phone
or acoustic-echo claim. Run after making scout-plant-scope.wav in out/audio-lab.
"""
import argparse
import asyncio
import json
import time
import httpx

from livekit_cloud_smoke import main as cloud_run, until, wav, ROOT
from livekit_camera_replay import captions, verify_result_audio


async def scenario(port,session,ws,queued,events,state,audio,report):
    report.update(real_bioclip=True,real_taxonomy=True,synthetic_phone=True)
    marker=time.monotonic();queued.extend(wav('scout-plant-scope.wav'))
    await until(lambda:any(e.get('type')=='camera_prepare' for e in events),45)
    prepare=next(e for e in events if e.get('type')=='camera_prepare')
    obs=next(e for e in reversed(events) if e.get('type')=='observation' and e.get('id')==prepare['obs'])
    assert obs['target_group']=='Plantae' and obs['group']=='Plantae',obs
    assert not any(e.get('type')=='capture' for e in events)
    report['checks'].append({'case':'Spoken explicit plant intent becomes a persistent plant target without automatic capture','passed':True})
    await ws.send(json.dumps({'type':'camera_ready','request_id':prepare['request_id']}))
    await until(lambda:any('camera ready' in t.lower() for t in captions(events,marker)),30)
    queued.extend(wav('camera-start.wav'))
    await until(lambda:any(e.get('type')=='capture' for e in events),35)
    capture=next(e for e in events if e.get('type')=='capture')
    # Resolve by recorded observation id; taxon ids can differ between snapshots.
    manifest=json.loads((ROOT/'out/vision-accuracy-20261025/manifest.json').read_text())
    item=next(r for r in manifest['images'] if r['observation_id']==410725303)
    photo=ROOT/'out/vision-accuracy-20261025'/item['image']
    marker=time.monotonic()
    async with httpx.AsyncClient(timeout=90) as client:
        response=await client.post(f'http://127.0.0.1:{port}/api/frame',
            params={'session':session,'obs':capture['obs'],'capture':capture['request_id']},content=photo.read_bytes())
        response.raise_for_status()
    await until(lambda:any(e.get('type')=='observation' and e.get('analysis_status')=='ready' for e in events),35)
    obs=next(e for e in reversed(events) if e.get('type')=='observation' and e.get('analysis_status')=='ready')
    assert obs['target_group']==obs['group']=='Plantae'
    assert obs['uncertain'] and obs['visual_assessment']['category_conflict']
    assert all(c.get('group')=='Plantae' for c in response.json()['detection']['candidates'])
    report['checks'].append({'case':'Real conflicting pine photo keeps plant target and stays uncertain with only plant candidates','passed':True})
    await until(lambda:any('conflicting evidence' in t.lower() for t in captions(events,marker)),50)
    frames=await verify_result_audio(events,audio,marker,'conflicting evidence')
    await until(lambda:not state.get('speaking'))
    assert not any(e.get('type')=='investigations' and e.get('items') for e in events)
    report['checks'].append({'case':'Conflict prompts for subject framing over real speech without silently starting expert research','received_audio_frames':frames,'passed':True})
    marker=time.monotonic();queued.extend(wav('scout-photo-status.wav'))
    await until(lambda:any('conflict' in t.lower() for t in captions(events,marker)),45)
    await until(lambda:not state.get('speaking'))
    report['checks'].append({'case':'Spoken progress query reports the category conflict without taking another photo','passed':True})
    marker=time.monotonic();queued.extend(wav('scout-photo-result.wav'))
    await until(lambda:any('conflicting evidence' in t.lower() for t in captions(events,marker)),45)
    frames=await verify_result_audio(events,audio,marker,'conflicting evidence')
    assert sum(e.get('type')=='capture' for e in events)==1
    report['checks'].append({'case':'Spoken result query preserves the clarification instead of presenting a guessed species as certain','received_audio_frames':frames,'passed':True})


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--port',type=int,default=8805)
    args=parser.parse_args()
    asyncio.run(cloud_run(args.port,scenario=scenario,report_name='visual-target-cloud-smoke.json'))
