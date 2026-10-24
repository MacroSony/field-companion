"""Offline templates, REST failure, and shared playback regressions."""
import asyncio
import json
import os
import time
import unittest
from unittest.mock import AsyncMock, patch

import server as s
from template_answers import build_answer, expand_numbers, number_words


class Templates(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(build_answer('recent_sounds', {'seconds': 30, 'detections': []}), 'Nothing new in the last half minute.')
        self.assertEqual(build_answer('get_checklist', {'items': []}), 'No species logged so far.')

    def test_one_and_plural(self):
        self.assertEqual(build_answer('get_checklist', {'items': [{'en': 'American Robin', 'count': 1}]}), 'One species logged so far: American Robin. One individual in total.')
        self.assertIn('two individuals', build_answer('get_checklist', {'items': [{'en': 'Robin', 'count': 2}]}).lower())
        self.assertIn('last one second', build_answer('recent_sounds', {'seconds': 1, 'detections': [{'en': 'Robin'}]}))

    def test_many_dedup_and_unconfirmed(self):
        text = build_answer('recent_sounds', {'seconds': 15, 'detections': [{'en': n} for n in ['Robin', 'Robin', 'Cardinal', 'Jay', 'Crow']]})
        self.assertEqual(text, 'The detector picked up Robin, Cardinal and Jay and one more species in the last fifteen seconds; these are unconfirmed.')
        text = build_answer('get_checklist', {'items': [{'en': x, 'count': 3} for x in ['A', 'B', 'C', 'D', 'E']]})
        self.assertEqual(text, 'Five species logged so far: A, B and C and two more. Fifteen individuals in total.')

    def test_numbers(self):
        self.assertEqual(number_words(1234), 'one thousand two hundred thirty-four')
        self.assertEqual(expand_numbers('15 seconds, 0.5 seconds, 21 birds'), 'fifteen seconds, zero point five seconds, twenty-one birds')
        self.assertIsNone(build_answer('recent_sounds', {'ok': False}))
        self.assertIsNone(build_answer('speak', {}))


class FakeStream:
    def __init__(self, mode):
        self.mode = mode
    async def chunks(self, *args):
        if self.mode == 'partial':
            yield b'\0\0' * 10
        if self.mode in ('fail', 'partial'):
            raise OSError('offline')
        if self.mode == 'timeout':
            raise asyncio.TimeoutError()
        if self.mode == 'ok':
            yield b'\0\0' * 10
    def close(self):
        pass


class Playback(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session = s.Session('template-test')
        self.session.connected = True
        self.session.rt_send = AsyncMock()
        self.session.phone_json = AsyncMock()
        self.session.phone_audio = AsyncMock()
        self.session.generate = AsyncMock()
        self.route = {'status': 'completed', 'output': [{'type': 'function_call', 'name': 'get_checklist', 'call_id': 'x', 'arguments': '{}'}]}
        self.env = patch.dict(os.environ, {'TEMPLATE_ANSWERS': '1'})
        self.env.start()

    async def asyncTearDown(self):
        self.env.stop()
        await self.session.close()

    async def test_fallback_failure_timeout_empty_partial(self):
        for mode in ['fail', 'timeout', 'empty', 'partial']:
            with self.subTest(mode=mode):
                self.session.tts = FakeStream(mode)
                self.session.generate.reset_mock()
                await self.session.finish_route(self.route)
                self.session.generate.assert_awaited_once_with('answer')
        self.assertIn(unittest.mock.call({'type': 'flush'}), self.session.phone_json.await_args_list)

    async def test_success_history_and_same_pcm_path(self):
        self.session.tts = FakeStream('ok')
        await self.session.finish_route(self.route)
        self.session.generate.assert_not_awaited()
        self.session.phone_audio.assert_awaited_once_with(b'\0\0' * 10)
        self.assertTrue(self.session.first_audio)
        self.assertGreater(self.session.echo_estimate, 0)
        history = self.session.rt_send.await_args_list[-1].args[0]['item']
        self.assertEqual(history['role'], 'assistant')
        self.assertEqual(history['content'][0]['text'], 'No species logged so far.')
        self.assertTrue(self.session.spoken)

    async def test_default_off_quiet_and_stale(self):
        self.session.tts = FakeStream('ok')
        with patch.dict(os.environ, {'TEMPLATE_ANSWERS': '0'}):
            await self.session.finish_route(self.route)
        self.session.generate.assert_awaited_once_with('answer')
        self.session.generate.reset_mock()
        self.session.quiet = True
        await self.session.finish_route(self.route)
        self.session.quiet = False
        self.session.turn_stopped_at = time.monotonic() - 13
        await self.session.finish_route(self.route)
        self.session.generate.assert_not_awaited()
        self.session.phone_audio.assert_not_awaited()

    async def test_cancel_pending_synthesis_never_falls_back(self):
        session = self.session
        class Cancelled(FakeStream):
            async def chunks(self, *args):
                session.audio_allowed = False
                yield b'\0\0'
        session.tts = Cancelled('ok')
        await session.finish_route(self.route)
        session.generate.assert_not_awaited()
        session.phone_audio.assert_not_awaited()
