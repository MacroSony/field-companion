"""State-based retry resolution and real camera/queue execution boundaries."""
import json
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import server as core
from expert import NaturalistExpert, Settings
from investigations import Investigation
from task_retry import select_retry_tasks
from test_server import FakePhone


class TaskRetryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session=core.Session('retry-test')
        self.session.expert=NaturalistExpert(Settings(enabled=False))
        self.phone=FakePhone();self.client=core.Client(self.phone,self.session)
        self.session.clients.add(self.client);self.session.mic=self.client
        self.session.connected=True
        self.session.rt_send=AsyncMock()
        self.session.play_feedback=AsyncMock(return_value=True)
        self.session.speak_expert=AsyncMock(return_value={'status':'completed'})
        self.session.generate=AsyncMock(return_value={'status':'completed'})
        self.session.turn_question='Okay, I want to retry.'
        self.session.investigations.submit=AsyncMock(return_value=SimpleNamespace(id='R3'))

    async def asyncTearDown(self):
        await self.session.cancel_camera_request()
        await self.session.close()

    async def photo(self,status='failed',group='Plantae'):
        obs=await self.session.new_focus(group)
        obs.analysis_status=obs.analysis_stage=status
        obs.analysis_request_id='failed-photo'
        obs.analysis_created_at='2026-10-25T12:00:00+00:00'
        obs.hint='the same plant'
        self.session.camera_request={'id':obs.analysis_request_id,'obs':obs.id,
            'phase':'ready' if status=='failed' else 'classifying','confirmed':False,'language':'en'}
        self.phone.events.clear()
        return obs

    def job(self,task_id='R1',status='failed',parent_id=None,origin='question'):
        job=Investigation(task_id,'What does the original animal eat?',
            {'id':'original-animal','fused':[{'sci':'Lynx rufus','en':'Bobcat'}]},
            status=status,stage=status,parent_id=parent_id,origin=origin,
            created_at=f'2026-10-25T11:0{task_id[-1]}:00+00:00')
        self.session.investigations.jobs[job.id]=job
        return job

    async def route(self,args=None,name='retry_task'):
        await self.session.finish_route({'status':'completed','output':[{
            'type':'function_call','name':name,'call_id':'retry-call','arguments':json.dumps(args or {})}]})
        outputs=[call.args[0]['item']['output'] for call in self.session.rt_send.await_args_list
                 if call.args[0].get('item',{}).get('type')=='function_call_output']
        return json.loads(outputs[-1])

    def no_camera_changes(self):
        self.assertFalse(any(event.get('type') in ('camera_prepare','capture','camera_cancelled') for event in self.phone.events))

    async def test_unqualified_retry_prepares_the_failed_photo_without_capture_or_new_investigation(self):
        obs=await self.photo()
        result=await self.route()
        self.assertTrue(result['ok'])
        self.assertEqual(self.session.focus,obs.id)
        self.assertEqual(obs.group,'Plantae')
        self.assertEqual(obs.hint,'the same plant')
        self.assertNotEqual(self.session.camera_request['id'],'failed-photo')
        self.assertFalse(self.session.camera_request['confirmed'])
        self.assertTrue(any(event.get('type')=='camera_prepare' for event in self.phone.events))
        self.assertFalse(any(event.get('type')=='capture' for event in self.phone.events))
        self.session.investigations.submit.assert_not_awaited()
        self.session.speak_expert.assert_not_awaited()
        request=self.session.camera_request
        await self.session.camera_ready({'request_id':request['id']})
        self.assertFalse(any(event.get('type')=='capture' for event in self.phone.events))
        await self.session.camera_confirmation('Start.')
        self.assertTrue(any(event.get('type')=='capture' for event in self.phone.events))

    async def test_explicit_investigation_retry_keeps_original_evidence_after_focus_changes(self):
        old=self.job()
        await self.photo()
        self.session.expert=SimpleNamespace(available=True)
        result=await self.route({'id':'R1'})
        self.assertEqual(result['investigation_id'],'R3')
        self.session.investigations.submit.assert_awaited_once_with(old.question,old.observation,origin='question',parent_id='R1')
        self.no_camera_changes()
        self.assertIsNone(self.session.retry_offer)

    async def test_two_failed_tasks_require_selection_without_starting_either(self):
        await self.photo();self.job()
        result=await self.route()
        self.assertTrue(result['requires_selection'])
        self.assertIn('Which task',result['answer'])
        self.no_camera_changes();self.session.investigations.submit.assert_not_awaited()
        self.session.last_assistant_text=result['answer']
        self.assertEqual(len(self.session.task_retry_context()['tasks']),2)
        result=await self.route({'scope':'camera'})
        self.assertTrue(result['waiting_for_confirmation'])

    async def test_latest_spoken_photo_failure_wins_over_an_unrelated_failed_investigation(self):
        await self.photo();self.job()
        result=await self.session.speak_task_status({'scope':'camera'})
        self.session.last_assistant_text=result['answer']
        result=await self.route()
        self.assertTrue(result['waiting_for_confirmation'])
        self.session.investigations.submit.assert_not_awaited()

    async def test_explicit_id_overrides_the_latest_spoken_photo_failure(self):
        await self.photo();self.job()
        result=await self.session.speak_task_status({'scope':'camera'})
        self.session.last_assistant_text=result['answer']
        self.session.expert=SimpleNamespace(available=True)
        result=await self.route({'id':'R1'})
        self.assertEqual(result['investigation_id'],'R3');self.no_camera_changes()

    async def test_unqualified_investigation_retry_works_with_no_photo(self):
        old=self.job();self.session.expert=SimpleNamespace(available=True)
        result=await self.route()
        self.assertEqual(result['investigation_id'],'R3')
        self.session.investigations.submit.assert_awaited_once_with(old.question,old.observation,origin='question',parent_id='R1')
        self.no_camera_changes()

    async def test_last_spoken_investigation_failure_wins_over_unrelated_photo_failure(self):
        await self.photo();self.job()
        self.session.turn_question='How is R1?'
        await self.route({'id':'R1'},name='get_investigations')
        answer=self.session.speak_expert.await_args.args[0]
        self.session.last_assistant_text=answer
        self.session.turn_question='Try again.'
        self.session.expert=SimpleNamespace(available=True)
        result=await self.route()
        self.assertEqual(result['investigation_id'],'R3');self.no_camera_changes()

    async def test_failed_investigation_notice_binds_the_next_unqualified_retry(self):
        await self.photo();job=self.job()
        await self.session.investigation_notice(job)
        self.session.last_assistant_text=self.session.speak_expert.await_args.args[0]
        self.session.expert=SimpleNamespace(available=True)
        result=await self.route()
        self.assertEqual(result['investigation_id'],'R3');self.no_camera_changes()

    async def test_newer_running_attempt_prevents_restarting_its_failed_parent(self):
        self.job();self.job('R2','running',parent_id='R1')
        result=await self.route()
        self.assertEqual(result['tasks'][0]['id'],'R2')
        self.session.investigations.submit.assert_not_awaited();self.no_camera_changes()

    async def test_running_photo_is_reported_without_cancelling_or_duplicate_capture(self):
        obs=await self.photo('analyzing');request=self.session.camera_request
        result=await self.route({'scope':'camera'})
        self.assertTrue(result['ok']);self.assertIs(self.session.camera_request,request)
        self.assertEqual(obs.analysis_status,'analyzing');self.no_camera_changes()

    async def test_unknown_id_never_falls_back_to_a_different_failed_task(self):
        await self.photo();self.job()
        result=await self.route({'id':'R404'})
        self.assertFalse(result['ok']);self.no_camera_changes()
        self.session.investigations.submit.assert_not_awaited()

    async def test_id_and_scope_mismatch_does_not_retry_another_task(self):
        await self.photo();self.job()
        result=await self.route({'id':'R1','scope':'camera'})
        self.assertFalse(result['ok']);self.no_camera_changes()

    async def test_old_photo_retry_cannot_replace_a_new_focus(self):
        await self.photo();new=await self.session.new_focus('Insecta');self.phone.events.clear()
        result=await self.route({'id':'photo:failed-photo'})
        self.assertFalse(result['ok']);self.assertEqual(self.session.focus,new.id);self.no_camera_changes()

    async def test_focus_change_during_acknowledgement_invalidates_photo_retry(self):
        await self.photo()
        async def changed(*args,**kwargs):
            await self.session.new_focus('Insecta')
            self.phone.events.clear()
            return True
        self.session.play_feedback.side_effect=changed
        result=await self.route()
        self.assertEqual(result['guard'],'stale_camera_context')
        self.assertEqual(self.session.observations[self.session.focus].group,'Insecta');self.no_camera_changes()

    async def test_stale_route_context_cannot_retry_a_replacement_photo(self):
        await self.photo();self.session.turn_context=self.session.input_context()
        await self.photo();self.phone.events.clear()
        result=await self.route()
        self.assertEqual(result['guard'],'stale_camera_context');self.no_camera_changes()

    async def test_expired_or_superseded_spoken_reference_does_not_choose_a_task(self):
        await self.photo();self.job()
        self.session.retry_offer={'question':'Try the photo again.','tasks':[{'id':'photo:failed-photo','kind':'camera'}],'at':time.monotonic()-46}
        self.session.last_assistant_text='Try the photo again.'
        result=await self.route();self.assertTrue(result['requires_selection'])
        self.session.retry_offer['at']=time.monotonic();self.session.last_assistant_text='A different answer.'
        self.assertIsNone(self.session.task_retry_context())

    async def test_photo_expert_failure_retries_the_linked_expert_with_original_origin(self):
        obs=await self.photo('ready');obs.uncertain=False
        job=self.job(origin='vision')
        job.observation={'id':obs.id,'fused':[],'image_revision':obs.image_revision,'analysis_request_id':obs.analysis_request_id}
        self.session.expert=SimpleNamespace(available=True)
        result=await self.route({'scope':'camera'})
        self.assertEqual(result['investigation_id'],'R3');self.no_camera_changes()
        self.assertEqual(self.session.investigations.submit.await_args.kwargs['origin'],'vision')

    async def test_spoken_linked_expert_failure_is_not_hidden_by_its_photo_alias(self):
        obs=await self.photo('ready');obs.uncertain=False
        job=self.job(origin='vision')
        job.observation={'id':obs.id,'fused':[],'image_revision':obs.image_revision,'analysis_request_id':obs.analysis_request_id}
        await self.session.investigation_notice(job)
        self.session.last_assistant_text=self.session.speak_expert.await_args.args[0]
        self.session.expert=SimpleNamespace(available=True)
        result=await self.route()
        self.assertEqual(result['investigation_id'],'R3');self.no_camera_changes()

    async def test_old_model_retry_tool_cannot_bypass_task_resolution(self):
        await self.photo()
        result=await self.route({'action':'retry','selection':'latest'},name='manage_investigation')
        self.assertFalse(result['ok']);self.session.investigations.submit.assert_not_awaited();self.no_camera_changes()

    async def test_chinese_ambiguity_is_explained_without_side_effects(self):
        await self.photo();self.job();self.session.turn_question='再试一次'
        result=await self.route()
        self.assertIn('照片识别',result['answer']);self.assertIn('调查 R1',result['answer']);self.no_camera_changes()

    async def test_empty_session_and_invalid_selectors_never_create_work(self):
        result=await self.route();self.assertFalse(result['ok']);self.no_camera_changes()
        for args in ({'scope':'not-a-scope'},{'selection':'guess'}):
            with self.assertRaises(ValueError):
                select_retry_tasks([],args)


if __name__=='__main__':
    unittest.main()
