"""Regressions from a real Scout -> photo -> result conversation.

Offline tests inject the classifier/network boundaries, not the application
state transitions. Billed scenario replay additionally checks real speech text.
"""
import asyncio
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from PIL import Image
import server as core
from expert import Settings
from realtime_livekit import make_session
from test_server import ASGIClient, FakePhone


class ConversationRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session = make_session(core.Session, 'conversation-regression')
        self.session.expert.settings = Settings(enabled=False)
        self.session.allowed_species = lambda: set()
        self.session.log = Mock()
        self.phone = FakePhone()
        self.client = core.Client(self.phone, self.session)
        self.session.mic = self.client
        self.session.clients.add(self.client)
        self.session.connected = True
        self.session.rt = SimpleNamespace(send=AsyncMock())
        self.session.voice = SimpleNamespace(ready=True, reply=AsyncMock(return_value={'status':'completed'}),
            say=AsyncMock(return_value=True), interrupt=AsyncMock(), close=AsyncMock())

    async def asyncTearDown(self):
        core.SESSIONS.pop(self.session.id, None)
        await self.session.close()

    async def test_addressing_service_failure_is_not_classified_as_chatter(self):
        await self.session.voice_answer('What do you need?')
        invitation = self.session.wake.pending
        self.session.conversation_services.classify_reply = AsyncMock(side_effect=TimeoutError())
        await self.session.voice_input('I saw an animal. Help me identify it.')
        self.assertIs(self.session.wake.pending, invitation)
        self.assertTrue(any(e.get('type') == 'reply_retry' for e in self.phone.events))
        self.assertFalse(self.session.pending)
        self.assertFalse(self.session.camera_request)
        self.session.voice.interrupt.assert_not_awaited()
        self.assertFalse(any(call.args[0] == 'input_ignored' and
            call.kwargs.get('reason') == 'not_answer_to_pending_question' for call in self.session.log.call_args_list))
        # A deliberate retry remains usable; no implicit photo was authorized.
        await self.session.voice_input('Scout, identify that animal.')
        self.assertTrue(self.session.pending[-1][1].addressed)

    async def test_photo_completion_is_a_result_readout_with_no_new_user_question(self):
        obs = await self.session.new_focus('Any')
        obs.fused = [{'sci':'Anser anser','en':'Greylag Goose','p':.99}]
        obs.uncertain = False
        obs.analysis_status = 'ready'
        obs.image_revision = 1
        self.session.dialogue.add('user', 'Start.')
        self.session.turn_question = ''  # This was the missing production path.
        await self.session.answer_vision(obs.id)
        instructions = self.session.voice.reply.await_args.args[0]
        self.assertIn('Greylag Goose', instructions)
        self.assertNotIn('call ignore', instructions)
        self.assertNotIn('Classify ONLY', instructions)
        self.assertIn((obs.id, 1), self.session.vision_answered)

    async def test_failed_or_interrupted_result_is_not_marked_as_announced(self):
        obs = await self.session.new_focus('Any')
        obs.fused = [{'sci':'Anser anser','en':'Greylag Goose','p':.99}]
        obs.uncertain = False
        obs.analysis_status = 'ready'
        self.session.voice.reply.return_value = {'status':'cancelled'}
        await self.session.answer_vision(obs.id)
        self.assertNotIn((obs.id, obs.image_revision), self.session.vision_answered)

    async def test_unspecified_new_photo_does_not_assume_a_taxonomic_group(self):
        result = await self.session.tool('look', {'target':'new'})
        self.assertEqual(result['observation']['group'], 'Any')
        result = await self.session.tool('look', {'target':'new','group':'Plantae'})
        self.assertEqual(result['observation']['group'], 'Plantae')

    async def test_exact_photo_taxon_resolves_without_a_guessed_group_and_repairs_it(self):
        await self.session.new_focus('Mammalia')
        await self.session.request_look()
        request = self.session.camera_request
        request['phase'] = 'ready'
        await self.session.queue_camera_start(request)
        obs = self.session.observations[self.session.focus]
        core.SESSIONS[self.session.id] = self.session
        image = io.BytesIO()
        Image.new('RGB',(64,64),'green').save(image,format='JPEG')
        fixture = {'candidates':[{'sci':'Anser anser','p':.99}], 'ms':10,'sharpness':5}
        taxon = {'sci':'Anser anser','en':'Greylag Goose','group':'Aves','photo':''}
        # An older failed name lookup must not poison every later photo.
        self.session.names['Anser anser'] = {'sci':'Anser anser','en':'Anser anser'}
        async with ASGIClient() as client:
            with patch('server.classify_frame',return_value=fixture), patch('server.resolve',return_value=taxon) as resolve:
                result = await client.post('/api/frame',params={'session':self.session.id,
                    'obs':obs.id,'capture':request['id']},content=image.getvalue())
                self.assertEqual(result.status_code,200,result.text)
                resolve.assert_called_once_with('Anser anser')
        self.assertEqual(obs.fused[0]['en'], 'Greylag Goose')
        self.assertEqual(obs.group, 'Aves')


if __name__ == '__main__':
    unittest.main()
