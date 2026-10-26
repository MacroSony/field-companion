import asyncio
import unittest
from unittest.mock import AsyncMock

from conversation_state import ScoutGate, SharedDialogue, ConversationServices, PendingReply
from unittest.mock import patch


class AddressingTests(unittest.TestCase):
    def setUp(self):
        self.now = 100
        self.gate = ScoutGate(lambda: self.now)

    def test_name_is_a_vocative_not_a_substring_or_mention(self):
        for text in ('Scout, what is that?', 'Hey Scout, what is that?', 'SCOUT: what is that?'):
            self.assertEqual(self.gate.addressed(text), 'what is that?')
        for text in ('What did Scout say?', 'I told Scout to look.', 'Scouting is fun', 'Scott, start', 'start'):
            self.assertIsNone(self.gate.addressed(text))
        self.assertEqual(self.gate.addressed('Scout!'), '')
        self.assertEqual(self.gate.addressed('Scout帮我看看那只鸟'), '帮我看看那只鸟')

    def test_only_latest_completed_question_grants_a_scoped_reply(self):
        self.gate.offer('Which task?', 'binding')
        self.assertIsNotNone(self.gate.current('binding'))
        self.gate.offer('I am still analyzing.', 'binding')
        self.assertIsNone(self.gate.current('binding'))
        self.gate.offer('Would you like the result?', 'binding', interrupted=True)
        self.assertIsNone(self.gate.current('binding'))
        self.gate.offer('You asked "which task?". I am still checking.', 'binding')
        self.assertIsNone(self.gate.current('binding'))

    def test_question_expires_and_state_changes_revoke_it(self):
        self.gate.offer('Start?', 'camera1')
        self.assertIsNone(self.gate.current('camera2'))
        self.gate.offer('Start?', 'camera2')
        self.now += 41
        self.assertIsNone(self.gate.current('camera2'))

    def test_camera_invitation_does_not_require_question_mark(self):
        self.gate.offer('Camera ready. Say start to take a photo.', 'c1', kind='camera')
        self.assertEqual(self.gate.current('c1').kind, 'camera')


class MemoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_older_dialogue_stays_visible_until_summary_succeeds(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def summarize(old, messages):
            started.set()
            await release.wait()
            return 'User prefers English and saw a plant near the pond.'
        memory = SharedDialogue(summarize, recent_limit=2)
        try:
            memory.add('user', 'Please use English.')
            memory.add('assistant', 'Certainly.')
            memory.add('user', 'I saw a plant near the pond.')
            await started.wait()
            self.assertEqual(memory.snapshot()['older_unsummarized'][0]['text'], 'Please use English.')
            memory.add('assistant', 'What would you like to know?')
            release.set()
            await memory.task
            self.assertFalse(memory.archive)
            self.assertIn('English', memory.summary)
            self.assertEqual(len(memory.recent), 2)
        finally:
            await memory.close()

    async def test_failed_summary_keeps_originals_and_cools_down(self):
        summarize = AsyncMock(side_effect=RuntimeError('offline'))
        now = [100]
        memory = SharedDialogue(summarize, recent_limit=1, clock=lambda: now[0])
        try:
            memory.add('user', 'Keep this preference.')
            memory.add('assistant', 'Okay.')
            await memory.task
            self.assertIn('Keep this preference.', str(memory.snapshot()))
            memory.add('user', 'Another request.')
            self.assertEqual(summarize.await_count, 1)
            now[0] += 31
            memory.add('assistant', 'Another answer.')
            await memory.task
            self.assertEqual(summarize.await_count, 2)
        finally:
            await memory.close()

    async def test_reset_cancels_inflight_summary_and_clears_both_windows(self):
        started = asyncio.Event()
        async def summarize(old, messages):
            started.set()
            await asyncio.Future()
        memory = SharedDialogue(summarize, recent_limit=1)
        memory.add('user', 'old')
        memory.add('assistant', 'older')
        await started.wait()
        await memory.reset()
        self.assertEqual(memory.snapshot(), {'summary':'', 'older_unsummarized':[], 'recent_dialogue':[], 'history_incomplete':False})
        memory.add('user', 'new')
        self.assertEqual(memory.recent[0]['text'], 'new')
        await memory.close()

    async def test_memory_is_bounded_and_reports_degraded_history(self):
        memory = SharedDialogue(recent_limit=1, archive_chars=20)
        for index in range(20):
            memory.add('user', f'Message {index}')
        self.assertTrue(memory.incomplete)
        self.assertLessEqual(sum(len(r['text']) for r in memory.archive), 20)
        await memory.close()


class ReplyDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_response_after_old_three_second_limit_is_still_usable(self):
        service = ConversationServices(None)
        async def slow_reply(*args):
            await asyncio.sleep(3.1)
            return {'kind':'answer','camera_action':'none'}
        service.classify_reply = slow_reply
        result = await service.classify_reply_with_deadline(PendingReply('What do you need?','',0), 'Identify that animal.')
        self.assertEqual(result['kind'], 'answer')

    async def test_deadline_cancels_slow_request_instead_of_leaking_work(self):
        service = ConversationServices(None)
        stopped = asyncio.Event()
        async def slow_reply(*args):
            try:
                await asyncio.Future()
            finally:
                stopped.set()
        service.classify_reply = slow_reply
        with patch('conversation_state.REPLY_TIMEOUT', .01):
            with self.assertRaises(TimeoutError):
                await service.classify_reply_with_deadline(PendingReply('Which one?','',0),'The first.')
        self.assertTrue(stopped.is_set())

if __name__ == '__main__':
    unittest.main()
