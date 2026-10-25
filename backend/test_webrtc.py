"""Offline media ownership, cleanup, echo policy and signaling regressions."""
import asyncio
import base64
import json
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx
import numpy as np

import server as s
from expert import Settings
from investigations import Investigation
from realtime_provider import RealtimeConfig
import realtime_webrtc as rtc
from test_server import FakePhone


SDP = "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\na=fingerprint:sha-256 TEST\r\n"


def config():
    return RealtimeConfig("openai", "gpt-realtime-2.1", "gpt-transcribe", "marin")


class ProtocolTests(unittest.TestCase):
    def test_call_locations_are_fixed_host_and_sanitized_id(self):
        self.assertEqual(rtc.call_id_from_location('/v1/realtime/calls/rtc_example-123'), 'rtc_example-123')
        self.assertEqual(rtc.call_id_from_location('https://api.openai.com/v1/realtime/calls/rtc_example'), 'rtc_example')
        for location in ('https://other.example/v1/realtime/calls/rtc_x', '/v1/realtime/calls/../secret',
                         '/v1/realtime/calls/rtc_x?token=x', '/v1/realtime/calls/no-prefix'):
            with self.assertRaises(ValueError):
                rtc.call_id_from_location(location)


class WebRTCTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.cls = type('TestRTC', (rtc.WebRTCSessionMixin, s.Session), {})
        self.c = self.cls('test-rtc', realtime=config())
        self.c.expert.settings = Settings(enabled=False)
        self.phone = FakePhone()
        self.client = s.Client(self.phone, self.c)
        self.client.playback_protocol = 1
        self.c.clients.add(self.client)
        self.c.mic = self.client
        self.c.rt_send = AsyncMock()
        self.hangup = patch.object(rtc, 'hangup_call', new_callable=AsyncMock).start()
        await self.c.mic_attached(self.client)
        self.c.allowed_species = lambda: set()

    async def asyncTearDown(self):
        await self.c.close()
        patch.stopall()

    async def test_capability_is_private_to_current_mic_and_single_use(self):
        token = self.client.rtc_token
        async def sideband(call_id):
            self.c.connected = True
            self.c.sideband_ready.set_result(True)
            await asyncio.Event().wait()
        self.c.sideband_loop = sideband
        with patch.object(rtc, 'create_call', AsyncMock(return_value=('rtc_example', SDP))) as create:
            answer = await self.c.offer(token, SDP)
            self.assertEqual(answer, SDP)
            create.assert_awaited_once()
            self.assertEqual(self.client.rtc_token, '')
            with self.assertRaises(PermissionError):
                await self.c.offer(token, SDP)
        self.assertEqual(self.phone.events[-1]['type'], 'webrtc_ready')

    async def test_stale_or_wrong_capability_never_creates_a_call(self):
        with patch.object(rtc, 'create_call', AsyncMock()) as create:
            with self.assertRaises(PermissionError):
                await self.c.offer('wrong-token', SDP)
            token = self.client.rtc_token
            self.client.rtc_token_created = time.monotonic() - 91
            with self.assertRaises(PermissionError):
                await self.c.offer(token, SDP)
            create.assert_not_awaited()

    async def test_replaced_phone_cannot_tear_down_current_call_or_receive_a_capability(self):
        stale = s.Client(FakePhone(), self.c)
        with patch.object(self.c, 'end_call', AsyncMock()) as end:
            await self.c.mic_attached(stale)
            end.assert_not_awaited()
        self.assertFalse(stale.phone.events)

    async def test_replacement_during_call_creation_hangs_up_orphan(self):
        async def create(*args):
            self.c.mic = s.Client(FakePhone(), self.c)
            return 'rtc_orphan', SDP
        with patch.object(rtc, 'create_call', create):
            with self.assertRaises(PermissionError):
                await self.c.offer(self.client.rtc_token, SDP)
        self.hangup.assert_awaited_with(self.c.realtime, 'rtc_orphan')

    async def test_environmental_pcm_never_duplicates_realtime_audio(self):
        self.c.connected = True
        self.client.duplex = True
        await self.c.ingest_audio(np.zeros(960, dtype='<i2').tobytes())
        self.assertEqual(self.c.position, 960)
        self.assertTrue(self.c.audio_queue.empty())

    async def test_environment_still_masks_assistant_playback_in_duplex(self):
        self.client.duplex = True
        self.client.playback_pending = True
        self.assertFalse(self.c.echo_guard)
        self.assertTrue(self.c.environment_contaminated())
        await self.c.ingest_audio(np.zeros(960, dtype='<i2').tobytes())
        self.assertTrue(self.c.echo_ring[:960].all())

    async def test_realtime_pcm_copy_is_not_played_but_cached_feedback_remains_available(self):
        self.c.phone_audio = AsyncMock()
        await self.c.handle_rt({'type': 'response.output_audio.delta', 'delta': base64.b64encode(bytes(2400)).decode()})
        self.c.phone_audio.assert_not_awaited()
        await self.c.play_feedback('camera-ready', language='en')
        self.c.phone_audio.assert_awaited_once()

    async def test_short_vad_burst_does_not_cancel_but_sustained_speech_does(self):
        self.client.duplex = True
        self.c.MIN_BARGE_SECONDS = .02
        self.c.active, self.c.phase = True, 'answer'
        self.c.cancel_speech = AsyncMock()
        record = {'id': 'human'}
        self.c.current_input_audio_turn = record
        self.c.user_speaking = True
        await self.c.voice_started(record)
        self.c.user_speaking = False
        await self.c.barge_task
        self.c.cancel_speech.assert_not_awaited()
        self.c.user_speaking = True
        await self.c.voice_started(record)
        await self.c.barge_task
        self.c.cancel_speech.assert_awaited_once()

    async def test_cancelling_voice_clears_remote_buffer_without_cancelling_research(self):
        self.c.connected, self.c.active, self.c.remote_playing = True, True, True
        self.c.phase, self.c.response_id, self.c.remote_response_id = 'answer', 'resp-current', 'resp-old'
        self.c.rt = SimpleNamespace()
        job = Investigation('R1', 'What does it eat?', {'id': 'bird', 'fused': []}, status='running')
        self.c.investigations.jobs[job.id] = job
        await self.c.cancel_speech()
        sent = [call.args[0]['type'] for call in self.c.rt_send.await_args_list]
        self.assertIn('output_audio_buffer.clear', sent)
        self.assertIn('response.cancel', sent)
        self.assertIn('resp-old', self.c.cancelled_ids)
        self.assertEqual(job.status, 'running')
        self.assertIn({'type': 'flush'}, self.phone.events)

    async def test_late_old_playout_stop_cannot_finish_new_reply(self):
        self.c.remote_response_id, self.c.remote_playing = 'new', True
        await self.c.handle_rt({'type': 'output_audio_buffer.stopped', 'response_id': 'old'})
        self.assertTrue(self.c.remote_playing)

    async def test_remote_playout_remains_pending_after_generation_and_early_client_drain(self):
        self.c.active = False
        self.c.remote_playing = True
        self.client.playback_active = self.client.playback_pending = False
        self.assertTrue(self.c.assistant_audio_pending())
        self.assertTrue(self.c.environment_contaminated())

    async def test_cancel_before_created_correlates_ack_without_closing_webrtc(self):
        old_socket = SimpleNamespace(close=AsyncMock())
        self.c.rt = old_socket
        future = asyncio.get_running_loop().create_future()
        self.c.cancel_barrier = {'future': future, 'unknown': True, 'response_id': None, 'rt': old_socket}
        self.c.generation = {'cancelled': True}
        await self.c.handle_rt({'type': 'response.created', 'response': {'id': 'late-cancelled'}})
        self.assertFalse(self.c.cancel_barrier['unknown'])
        await self.c.handle_rt({'type': 'response.done', 'response': {'id': 'late-cancelled', 'status': 'cancelled'}})
        token = {'cancelled': False, 'cancel': asyncio.get_running_loop().create_future()}
        await self.c.wait_cancel_boundary(token)
        self.assertIsNone(self.c.cancel_barrier)
        old_socket.close.assert_not_awaited()
        self.assertIs(self.c.rt, old_socket)
        self.c.generation = None

    async def test_repeating_assistant_words_is_not_removed_by_text_similarity_in_duplex(self):
        self.client.duplex = True
        self.c.spoken.append((('response', 'item', 'audio'), 'The bird is a Northern Cardinal.', time.monotonic()))
        self.assertFalse(self.c.transcript_is_echo('The bird is a Northern Cardinal.'))

    async def test_diagnostics_are_opt_in_and_exclude_secrets_coordinates_and_text(self):
        self.c.log('test', text='private words', token='private-token')
        self.assertFalse(self.c.diagnostic_tasks)
        await self.c.handle_phone({'type': 'diagnostics', 'enabled': True}, self.client)
        self.c.log('test', response_id='response', text='private words', token='private-token', lat=43, arguments={'name': 'secret'})
        await asyncio.gather(*self.c.diagnostic_tasks)
        rows = [r for r in self.phone.events if r.get('type') == 'diagnostic' and r.get('event') == 'test']
        self.assertEqual(rows[0]['fields'], {'response_id': 'response'})
        display = s.Client(FakePhone(), self.c, role='display')
        with self.assertRaises(ValueError):
            await self.c.handle_phone({'type': 'diagnostics', 'enabled': True}, display)

    async def test_signaling_api_is_disabled_by_default_bounded_and_sanitized(self):
        previous = getattr(s.app.state, 'enable_webrtc', False)
        s.SESSIONS['test-rtc'] = self.c
        self.c.offer = AsyncMock(return_value=SDP)
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=s.app), base_url='http://test') as http:
                s.app.state.enable_webrtc = False
                response = await http.post('/api/realtime/webrtc?session=test-rtc', content=SDP)
                self.assertEqual(response.status_code, 404)
                self.c.offer.assert_not_awaited()
                s.app.state.enable_webrtc = True
                self.assertEqual((await http.post('/api/realtime/webrtc?session=test-rtc', content=bytes(65537))).status_code, 413)
                self.assertEqual((await http.post('/api/realtime/webrtc?session=test-rtc', content='invalid')).status_code, 400)
                response = await http.post('/api/realtime/webrtc?session=test-rtc', content=SDP, headers={'x-field-rtc': 'socket-token'})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers['cache-control'], 'no-store')
                self.c.offer.assert_awaited_once_with('socket-token', SDP)
                self.c.offer.side_effect = RuntimeError('provider-secret-and-sdp')
                response = await http.post('/api/realtime/webrtc?session=test-rtc', content=SDP)
                self.assertEqual(response.status_code, 502)
                self.assertNotIn('provider-secret', response.text)
        finally:
            s.app.state.enable_webrtc = previous
            s.SESSIONS.pop('test-rtc', None)


if __name__ == '__main__':
    unittest.main()
