"""Opt-in real GPT routing + LiveKit playback, using synthetic in-memory app data.

No public listener, Cloud room, adaptive inference or physical echo test. API
calls are billed. Taxonomy is a fixture; no recognition accuracy is evaluated.
"""
import asyncio
import contextvars
import json
import logging
from pathlib import Path
from unittest.mock import patch

from livekit.agents import utils

import server as core
from livekit_voice import LiveKitVoice, RoutedVoiceAgent, create_session
from realtime_livekit import make_session
from test_livekit_sdk import MemorySink
from test_server import FakePhone

ROOT=Path(__file__).resolve().parents[1]


async def until(predicate,seconds=30):
    async with asyncio.timeout(seconds):
        while not predicate():
            await asyncio.sleep(.05)


async def cases(owner,voice,phone,client,report,events):
    voice.context=contextvars.copy_context()
    voice.session=create_session(owner.realtime,adaptive=False,wake_required=True)
    voice.bind_session_events()
    sink=MemorySink();voice.session.output.audio=sink
    voice.agent=RoutedVoiceAgent()
    await voice.session.start(voice.agent,record=False,session_host=False)
    voice.ready=True
    owner.start()
    owner.routing_task=asyncio.create_task(owner.realtime_loop())
    owner.tasks.append(owner.routing_task)
    await until(lambda:owner.connected)
    fixture={'sci':'Anas platyrhynchos','en':'Mallard','zh':'绿头鸭','photo':'','group':'Aves'}
    with patch('server.resolve',return_value=fixture):
        await owner.voice_input('Scout, two mallards on the pond.')
        await until(lambda:bool(owner.items) and not owner.turn_in_progress)
    assert owner.items[0]['count']==2
    assert sink.frames==0, 'Logging unexpectedly spoke'
    assert any(event.get('type')=='earcon' for event in phone.events)
    report['checks'].append({'case':'Real GPT route logs two mallards silently with an earcon','passed':True})

    await owner.voice_input('Scout, how many mallards have I logged?')
    await until(lambda:sink.frames>0 and not owner.turn_in_progress,45)
    await until(lambda:bool(owner.last_assistant_text))
    assert any(word in owner.last_assistant_text.lower() for word in ('two','2'))
    assert not phone.audio, 'Old WebSocket PCM leaked alongside LiveKit'
    report['checks'].append({'case':'Real tool results produce a grounded GPT answer through LiveKit only','passed':True})

    memory_before=owner.dialogue.snapshot()
    await owner.voice_input('Did you bring the snacks?')
    assert owner.dialogue.snapshot()==memory_before, 'Bystander speech entered shared memory'
    assert not owner.pending
    report['checks'].append({'case':'Unaddressed question does not reach routing or accepted memory','passed':True})

    previous=owner.last_assistant_text
    await owner.voice_input('Scout.')
    await until(lambda:owner.last_assistant_text!=previous and not owner.turn_in_progress,45)
    assert owner.wake.current(owner.reply_binding()), 'Unclear named request did not ask a question'
    assert not owner.camera_request and len(owner.items)==1
    await owner.voice_input('Tell me how many mallards are on the list.')
    await until(lambda:not owner.turn_in_progress and not owner.pending,45)
    assert any(word in owner.last_assistant_text.lower() for word in ('two','2')), 'Clarification reply did not reach a checklist answer'
    report['checks'].append({'case':'Calling Scout without intent asks for clarification and accepts a relevant name-free reply','passed':True})

    # Synthetic accepted-history fixture, avoiding 30 extra billed spoken turns.
    # Both real GPT connections must use the same compacted application memory.
    owner.dialogue.add('user', 'I call the duck beside the east pond Willow.')
    owner.dialogue.add('assistant', 'I will remember that nickname.')
    for number in range(15):
        owner.dialogue.add('user', f'I am passing trail marker {number}.')
        owner.dialogue.add('assistant', f'Understood, trail marker {number}.')
        await asyncio.sleep(0)
    if owner.dialogue.task:
        await owner.dialogue.task
    assert owner.dialogue.summary and 'Willow' in owner.dialogue.summary
    previous=owner.last_assistant_text
    await owner.voice_input('Scout, what nickname did I give the duck beside the east pond?')
    await until(lambda:owner.last_assistant_text!=previous and not owner.turn_in_progress,45)
    assert 'willow' in owner.last_assistant_text.lower(), 'Realtime answer did not recall compacted older dialogue'
    report['checks'].append({'case':'Real Realtime router and voice recall an early nickname after 30 newer accepted-history fixture messages','passed':True})

    await owner.voice_input('Scout, what is that animal? Please open the camera.')
    await until(lambda:owner.camera_request and not owner.turn_in_progress)
    request=owner.camera_request
    assert request['phase']=='opening'
    assert not any(event.get('type')=='capture' for event in phone.events)
    await owner.handle_phone({'type':'camera_ready','request_id':request['id']},client)
    await until(lambda:not owner.pending and not owner.turn_in_progress)
    await owner.voice_input('Start.')
    assert any(event.get('type')=='capture' for event in phone.events)
    report['checks'].append({'case':'Camera waits for readiness and explicit Start before sending capture','passed':True})

    # Simulate a classifier/service failure, then exercise the actual GPT route,
    # application retry resolver and LiveKit cue playback together.
    await owner.photo_failed(owner.observations[request['obs']],request)
    captures=sum(event.get('type')=='capture' for event in phone.events)
    await owner.voice_input('Scout, okay, I want to retry.')
    await until(lambda:owner.camera_request and owner.camera_request['id']!=request['id'] and not owner.turn_in_progress)
    retry_request=owner.camera_request
    assert retry_request['obs']==request['obs'] and retry_request['phase']=='opening'
    assert sum(event.get('type')=='capture' for event in phone.events)==captures
    assert not owner.investigations.jobs, 'Photo retry accidentally scheduled expert work'
    await owner.handle_phone({'type':'camera_ready','request_id':retry_request['id']},client)
    await until(lambda:not owner.pending and not owner.turn_in_progress)
    await owner.voice_input('Start.')
    assert sum(event.get('type')=='capture' for event in phone.events)==captures+1
    report['checks'].append({'case':'Failed photo retry prepares the same subject and recaptures only after a new Start','passed':True})
    assert not any(event['event']=='turn_failed' for event in events)


async def main():
    report={'passed':False,'physical_phone':False,'acoustic_echo_test':False,
            'adaptive_cloud_inference_test':False,'taxonomy_fixture':True,'checks':[]}
    owner=make_session(core.Session,'private-livekit-app-smoke')
    events=[]
    owner.log=lambda event,**fields:events.append({'event':event,**fields})
    phone=FakePhone();client=core.Client(phone,owner)
    owner.clients.add(client);owner.mic=client
    owner.allowed_species=lambda:{'Anas platyrhynchos'}
    voice=LiveKitVoice(owner,None,owner.realtime,adaptive=False)
    owner.voice=voice
    class Errors(logging.Handler):
        def __init__(self):
            super().__init__(logging.ERROR)
            self.count=0
        def emit(self,record):
            self.count+=1
    errors=Errors();logging.getLogger('livekit').addHandler(errors)
    async with utils.http_context.open():
        try:
            await cases(owner,voice,phone,client,report,events)
            report['passed']=True
        except Exception as error:
            report['error_type']=type(error).__name__
            if isinstance(error,AssertionError):
                report['assertion']=str(error)
            report['last_assistant']=owner.last_assistant_text
            report['recent_routes']=[event for event in events if event['event'] in ('route_done','tool','assistant_transcript')][-8:]
            report['reply_events']=[event for event in events if event['event'] in ('input_ignored','reply_addressing_unavailable','reply_addressing')]
            report['pending_reply']=owner.wake.pending.question if owner.wake.pending else None
            report['failed_events']=[event['event'] for event in events if 'failed' in event['event'] or 'error' in event['event']]
        finally:
            await owner.close()
            if voice.session:
                await voice.session.aclose()
    logging.getLogger('livekit').removeHandler(errors)
    report['sdk_runtime_errors']=errors.count
    report['passed']=report['passed'] and errors.count==0
    output=ROOT/'out/livekit/app-smoke.json'
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report))
    return 0 if report['passed'] else 1


if __name__=='__main__':
    raise SystemExit(asyncio.run(main()))
