"""Offline fake-Realtime regressions for round-two recovery."""
import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch
import server as s


class ReliabilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.c = s.Session('reliability')
        self.c.update_session = AsyncMock()
        self.c.phone_json = AsyncMock()
        self.c.state = AsyncMock()
        self.c.connected = True
        self.events = []
        self.statuses = []
        self.contexts = []
        async def send(raw):
            event = json.loads(raw)
            self.events.append(event)
            if event['type'] == 'conversation.item.create':
                await self.c.handle_rt({'type': 'conversation.item.added', 'item': event['item']})
            elif event['type'] == 'response.create':
                self.contexts.append(list(self.c.conversation_ids))
                n = len(self.contexts)
                await self.c.handle_rt({'type': 'response.created', 'response': {'id': f'r{n}'}})
                await self.c.handle_rt({'type': 'conversation.item.added', 'item': {'id': f'out{n}', 'type': 'function_call'}})
                await self.c.handle_rt({'type': 'response.done', 'response': {
                    'id': f'r{n}', 'status': self.statuses.pop(0), 'output': []}})
        self.c.rt = type('FakeRT', (), {'send': staticmethod(send)})()

    async def asyncTearDown(self):
        await self.c.close()

    async def wait_idle(self):
        for _ in range(200):
            if not self.c.pending and not self.c.turn_in_progress:
                return
            await asyncio.sleep(.01)
        self.fail(f'worker did not become idle: {self.c.pending}, {self.c.turn_task.get_stack()}, {self.events}')

    async def test_failed_route_retries_once_success(self):
        self.statuses = ['failed', 'completed']
        self.c.turn_items = set()
        user = await self.c.inject('Two mallards on the pond.')
        result = await self.c.generate('route')
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(len(self.contexts), 2)
        self.assertIn(user, self.contexts[-1])

    async def test_failed_twice_deletes_only_owned_items_preserves_new_audio(self):
        self.statuses = ['failed', 'failed', 'completed']
        self.c.finish_route = AsyncMock()
        original_send = self.c.rt.send
        queued = False
        async def send(raw):
            nonlocal queued
            event = json.loads(raw)
            if event['type'] == 'response.create' and not queued:
                queued = True
                await self.c.handle_rt({'type': 'conversation.item.added', 'item': {'id': 'newaudio', 'role': 'user'}})
                self.c.pending.append(('text', s.TurnText('How many species?', ['newaudio']), None))
            await original_send(raw)
        self.c.rt.send = send
        self.c.pending.append(('text', 'Two mallards on the pond.', None))
        self.c.turn_task = asyncio.create_task(self.c.turn_worker())
        self.c.tasks.append(self.c.turn_task)
        self.c.work_ready.set()
        await self.wait_idle()
        deleted = {e['item_id'] for e in self.events if e['type'] == 'conversation.item.delete'}
        self.assertTrue({'out1', 'out2'} <= deleted)
        self.assertNotIn('newaudio', deleted)
        first_user = self.contexts[0][0]
        self.assertIn(first_user, deleted)
        self.assertNotIn(first_user, self.contexts[2])
        self.assertIn('newaudio', self.contexts[2])
        self.assertTrue(deleted.isdisjoint(self.c.conversation_ids))
        self.c.finish_route.assert_awaited_once()

    async def test_filler_guard_silent_deletes_user_without_routing(self):
        self.c.generate = AsyncMock()
        self.c.tool = AsyncMock()
        self.c.suggestions = {"pending": {}}
        self.c.pending.extend(('text', text, None) for text in ['Ah.', 'Uh.', 'Um, hmm!', 'Mm-hmm.', 'mhm er erm oh mm'])
        self.c.turn_task = asyncio.create_task(self.c.turn_worker())
        self.c.tasks.append(self.c.turn_task)
        self.c.work_ready.set()
        await self.wait_idle()
        self.c.generate.assert_not_awaited()
        self.c.tool.assert_not_awaited()
        self.c.phone_json.assert_not_awaited()
        self.assertFalse(self.c.conversation_ids)
        self.assertEqual(len([e for e in self.events if e['type'] == 'conversation.item.delete']), 5)
        for text in ['Okay.', 'yes', 'yeah', 'sure', 'Ah, add it', '']:
            self.assertFalse(self.c.filler_only(text), text)

    async def test_remove_all_pair_only_and_ordered(self):
        def call(name, args):
            return {'type': 'function_call', 'name': name, 'call_id': name, 'arguments': json.dumps(args)}
        self.c.tool = AsyncMock(return_value={'ok': True})
        self.c.broadcast_checklist = AsyncMock()
        pair = [call('remove_observation', {'all': True}), call('dismiss_suggestions', {})]
        await self.c.finish_route({'status': 'completed', 'output': pair})
        self.assertEqual([c.args[0] for c in self.c.tool.call_args_list], ['dismiss_suggestions', 'remove_observation'])
        self.assertEqual(len([e for e in self.events if e['type'] == 'conversation.item.create']), 2)
        for other in [[call('remove_observation', {'all': False}), pair[1]],
                      [call('remove_observation', {'all': 'true'}), pair[1]],
                      [pair[0], call('add_observation', {})], pair + [pair[1]]]:
            self.c.tool.reset_mock()
            await self.c.finish_route({'status': 'completed', 'output': other})
            self.c.tool.assert_not_awaited()

    async def test_audit_args_compact_bounded_and_redacted(self):
        self.assertEqual(self.c.audit_args('{"count": 2, "all": true}'), '{"count":2,"all":true}')
        self.assertEqual(len(self.c.audit_args(json.dumps({'notes': 'x' * 300}))), 200)
        self.assertNotIn('private', self.c.audit_args('{"api_key":"private","nested":{"token":"private"}}'))
        self.assertEqual(self.c.audit_args('invalid private'), '[invalid JSON]')
        with patch.object(self.c, 'log') as log:
            await self.c.finish_route({'status': 'completed', 'output': [
                {'type': 'function_call', 'name': 'ignore', 'call_id': 'c', 'arguments': '{"count":2}'}]})
        self.assertEqual(log.call_args_list[0].kwargs['arguments'], ['{"count":2}'])
        self.assertEqual(log.call_args_list[-1].kwargs['arguments'], '{"count":2}')

    def test_priority_prompt_is_route_only(self):
        self.assertTrue(s.ROUTING.startswith('HIGHEST-PRIORITY ROUTING RULE:'))
        self.assertNotIn('HIGHEST-PRIORITY', s.ANSWER)
        tools = {t['name']: t['description'] for t in s.TOOLS}
        self.assertIn('explicit yes/yeah/sure', tools['accept_suggestions'])
        self.assertIn('never accept or dismiss', tools['ignore'])
        self.assertIn('Never for lone', tools['speak'])

    async def test_answer_and_post_tool_failures_never_retry(self):
        for phase, executed in [('answer', False), ('route', True)]:
            self.statuses = ['failed']
            self.contexts.clear()
            self.c.tools_executed = executed
            with self.assertRaises(RuntimeError):
                await self.c.generate(phase)
            self.assertEqual(len(self.contexts), 1)


if __name__ == '__main__':
    unittest.main()
