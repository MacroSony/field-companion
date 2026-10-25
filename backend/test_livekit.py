"""Offline application ownership and security tests; no LiveKit Cloud required."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
import httpx
import numpy as np

import server as core
from birdnet_worker import build_app
from expert import Settings
from investigations import Investigation
from livekit_settings import LiveKitSettings, save_local
from livekit_setup import install_setup
from realtime_livekit import LiveKitSessionMixin
from realtime_provider import RealtimeConfig
from test_server import FakePhone


class LocalSettingsTests(unittest.TestCase):
    def test_secrets_are_not_in_repr_and_urls_cannot_leak_credentials(self):
        config = LiveKitSettings('wss://example.livekit.cloud', 'api-key', 'a' * 40)
        config.validate()
        self.assertNotIn('api-key', repr(config))
        self.assertNotIn('a' * 40, repr(config))
        for url in ('https://example.com', 'wss://example.com', 'wss://key@example.livekit.cloud',
                    'wss://example.livekit.cloud/?secret=abc'):
            with self.assertRaises(ValueError):
                LiveKitSettings(url, 'key', 'x' * 40).validate()

    def test_local_configuration_roundtrip_does_not_overwrite_openai_key(self):
        from dotenv import dotenv_values
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.env.livekit.local'
            save_local(LiveKitSettings('wss://example.livekit.cloud', 'test-key', 's' * 40), path)
            saved = dotenv_values(path)
            self.assertEqual(set(saved), {'LIVEKIT_URL','LIVEKIT_API_KEY','LIVEKIT_API_SECRET'})
            self.assertEqual(saved['LIVEKIT_API_SECRET'], 's' * 40)
            self.assertFalse(path.with_name(path.name + '.tmp').exists())


class SetupTests(unittest.IsolatedAsyncioTestCase):
    async def test_setup_routes_precede_an_existing_catch_all_web_mount(self):
        from starlette.staticfiles import StaticFiles
        with tempfile.TemporaryDirectory() as directory:
            app=FastAPI()
            app.mount('/',StaticFiles(directory=directory))
            install_setup(app,lambda:False,write_link=False)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app,client=('127.0.0.1',1)),base_url='http://localhost') as client:
                response=await client.get('/setup/livekit')
                self.assertEqual(response.status_code,200)
                self.assertIn('Save locally',response.text)

    async def test_setup_is_loopback_only_capability_gated_and_never_returns_a_secret(self):
        app = FastAPI()
        install_setup(app, lambda: False, write_link=False)
        transport = httpx.ASGITransport(app=app, client=('127.0.0.1',12345))
        async with httpx.AsyncClient(transport=transport, base_url='http://localhost') as client:
            body = {'url':'wss://example.livekit.cloud','api_key':'private-key','api_secret':'s' * 40}
            self.assertEqual((await client.post('/api/livekit/setup', json=body)).status_code, 403)
            with patch('livekit_setup.save_local') as save, patch.object(LiveKitSettings, 'bind_inference_environment'):
                response = await client.post('/api/livekit/setup', json=body,
                    headers={'x-field-setup':app.state.livekit_setup_token})
                self.assertEqual(response.status_code, 200)
                self.assertNotIn('private-key', response.text)
                save.assert_called_once()
            response = await client.get('/setup/livekit', headers={'host':'public.example.com'})
            self.assertEqual(response.status_code, 404)
        remote = httpx.ASGITransport(app=app, client=('203.0.113.9',12345))
        async with httpx.AsyncClient(transport=remote, base_url='http://localhost') as client:
            self.assertEqual((await client.get('/setup/livekit')).status_code, 404)

    async def test_project_cannot_change_while_voice_is_active(self):
        app = FastAPI()
        install_setup(app, lambda: True, write_link=False)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=('127.0.0.1',1)), base_url='http://localhost') as client:
            response = await client.post('/api/livekit/setup', json={}, headers={'x-field-setup':app.state.livekit_setup_token})
            self.assertEqual(response.status_code,409)


class LiveKitControllerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        cls = type('TestLiveKit', (LiveKitSessionMixin, core.Session), {})
        self.session = cls('test-livekit', realtime=RealtimeConfig('openai','gpt-realtime-2.1','gpt-transcribe','marin'))
        self.session.expert.settings = Settings(enabled=False)
        self.phone = FakePhone()
        self.client = core.Client(self.phone,self.session)
        self.session.clients.add(self.client)
        self.session.mic = self.client
        self.session.allowed_species = lambda: set()
        self.session.rt = SimpleNamespace(send=AsyncMock())
        self.voice = SimpleNamespace(ready=True, reply=AsyncMock(return_value={'status':'completed','output':[]}),
            say=AsyncMock(return_value=True), interrupt=AsyncMock(), close=AsyncMock())
        self.session.voice = self.voice
        self.session.connected = True

    async def asyncTearDown(self):
        self.session.voice_room = None
        await self.session.close()

    async def test_only_livekit_plays_cues_and_realtime_pcm_is_not_forwarded(self):
        await self.session.play_feedback('camera-ready',language='en')
        self.voice.say.assert_awaited_once()
        self.assertFalse(self.phone.audio)
        await self.session.phone_audio(bytes(2400))
        self.assertFalse(self.phone.audio)

    async def test_authoritative_tool_results_are_passed_to_the_voice_model(self):
        self.session.turn_question = 'How many birds?'
        await self.session.rt_send({'type':'conversation.item.create','item':{
            'type':'function_call_output','output':'{"count":2,"species":"Mallard"}'}})
        response = await self.session.generate('answer')
        self.assertEqual(response['status'],'completed')
        context = json.loads(self.voice.reply.await_args.args[1])
        self.assertEqual(context['user_request'],'How many birds?')
        self.assertIn('Mallard',json.dumps(context))
        self.assertEqual(self.session.phase,'answer')

    async def test_ignored_input_does_not_cancel_speech_or_start_a_tool(self):
        self.session.handle_phone = AsyncMock()
        await self.session.voice_input('Okay.')
        self.session.handle_phone.assert_not_awaited()
        self.voice.interrupt.assert_not_awaited()

    async def test_accepted_speech_uses_existing_camera_confirmation(self):
        self.session.handle_phone = AsyncMock()
        await self.session.voice_input('Start.')
        self.session.handle_phone.assert_awaited_once_with({'type':'text','text':'Start.'},self.client)

    async def test_framework_playback_blocks_pending_notifications_and_masks_birdnet(self):
        await self.session.voice_state('speaking')
        self.assertTrue(self.session.playback_pending())
        self.assertTrue(self.session.environment_contaminated())
        await self.session.ingest_audio(np.zeros(960,dtype='<i2').tobytes())
        self.assertTrue(self.session.audio_queue.empty())
        self.assertTrue(self.session.echo_ring[:960].all())

    async def test_end_of_speech_does_not_release_notifications_before_transcription(self):
        await self.session.voice_user_state('speaking')
        await self.session.voice_user_state('listening')
        self.assertFalse(self.session.user_speaking)
        self.assertTrue(self.session.awaiting_transcript)
        await self.session.voice_input('Okay.')
        self.assertFalse(self.session.awaiting_transcript)

    async def test_framework_false_interruption_releases_notification_wait(self):
        await self.session.voice_user_state('speaking')
        await self.session.voice_user_state('listening')
        await self.session.voice_false_interruption()
        self.assertFalse(self.session.awaiting_transcript)
        self.assertIsNone(self.session.transcript_guard_task)

    async def test_interrupt_does_not_cancel_background_investigation(self):
        job = Investigation('R1','What does it eat?',{'id':'bird','fused':[]},status='running')
        self.session.investigations.jobs[job.id] = job
        self.session.phase = 'answer'
        await self.session.cancel_speech()
        self.voice.interrupt.assert_awaited_once()
        self.assertEqual(job.status,'running')

    async def test_voice_transcript_updates_router_context_and_consent_state(self):
        await self.session.voice_answer('Investigation R1 is ready. Would you like me to explain?')
        self.assertIn('R1',self.session.last_assistant_text)
        sent = json.loads(self.session.rt.send.await_args.args[0])
        self.assertEqual(sent['item']['role'],'assistant')
        self.assertEqual(sent['item']['content'][0]['type'],'output_text')

    async def test_wrong_capability_cannot_mint_a_room_token(self):
        self.client.livekit_token='private'
        self.client.livekit_token_created=0
        with self.assertRaises(PermissionError):
            await self.session.offer('wrong')

    async def test_expired_capability_cannot_start_a_room(self):
        self.client.livekit_token='private'
        self.client.livekit_token_created=0
        with self.assertRaises(PermissionError):
            await self.session.offer('private')
        self.assertIs(self.session.voice,self.voice)

    async def test_missing_project_does_not_consume_capability_or_start_model(self):
        import time
        self.client.livekit_token='private'
        self.client.livekit_token_created=time.monotonic()
        with patch.object(LiveKitSettings,'load',return_value=LiveKitSettings()):
            with self.assertRaises(ValueError):
                await self.session.offer('private')
        self.assertEqual(self.client.livekit_token,'private')
        self.assertIsNone(self.session.voice_task)

    async def test_replaced_mic_cannot_close_current_voice(self):
        stale=core.Client(FakePhone(),self.session)
        await self.session.mic_attached(stale)
        self.voice.close.assert_not_awaited()

    async def test_ambient_inference_calls_private_worker_without_processing_voice_twice(self):
        samples=np.zeros(48000,dtype=np.float32)
        with patch('realtime_livekit.worker_request',return_value={'detections':[{'confidence':.9}]}) as worker:
            result=self.session.analyze(samples)
            self.assertEqual(result,[{'confidence':.9}])
            self.assertIs(worker.call_args.kwargs['samples'],samples)


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.model=core.Session('worker-test')
        self.model.analyze=lambda samples:[{'en':'Mallard','confidence':.95}]
        self.model.allowed_species=lambda:{'Anas platyrhynchos'}
        with patch('birdnet_worker.core.Session',return_value=self.model):
            self.app=build_app('private-worker-key')
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url='http://localhost')
        self.headers={'x-field-worker':'private-worker-key'}

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.model.close()

    async def test_no_inference_or_range_lookup_without_worker_authorization(self):
        response=await self.client.post('/analyze?lat=43&lng=-79',content=bytes(12))
        self.assertEqual(response.status_code,403)
        self.assertEqual((await self.client.get('/allowed?lat=43&lng=-79')).status_code,403)

    async def test_inference_preserves_coordinates_and_model_results(self):
        response=await self.client.post('/analyze?lat=43&lng=-79',headers=self.headers,
            content=np.zeros(48000,dtype='<f4').tobytes())
        self.assertEqual(response.json()['detections'][0]['en'],'Mallard')
        self.assertEqual((self.model.lat,self.model.lng),(43,-79))
        response=await self.client.get('/allowed?lat=43&lng=-79',headers=self.headers)
        self.assertEqual(response.json()['species'],['Anas platyrhynchos'])

    async def test_malformed_or_nonfinite_audio_and_coordinates_are_rejected(self):
        for body in (b'',b'bad',np.array([float('nan')],dtype='<f4').tobytes(),
                     np.array([5.0],dtype='<f4').tobytes()):
            response=await self.client.post('/analyze?lat=43&lng=-79',headers=self.headers,content=body)
            self.assertEqual(response.status_code,422)
        response=await self.client.get('/allowed?lat=nan&lng=-79',headers=self.headers)
        self.assertEqual(response.status_code,422)

    async def test_oversized_audio_is_rejected_before_inference(self):
        response=await self.client.post('/analyze?lat=43&lng=-79',headers=self.headers,
            content=bytes(10*core.SR*4+4))
        self.assertEqual(response.status_code,413)


if __name__ == '__main__':
    unittest.main()
