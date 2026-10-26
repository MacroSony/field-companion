"""Target intent, visual conflicts and conservative confidence regressions."""
import io
import unittest
from unittest.mock import AsyncMock, Mock, patch
from PIL import Image

import server as core
from expert import Settings
from test_server import ASGIClient, FakePhone
from vision_policy import assess_visual, iconic_group


class PolicyTests(unittest.TestCase):
    def test_taxonomy_not_keyword_exceptions_covers_all_supported_groups(self):
        self.assertEqual(iconic_group(['Plantae','','','']), 'Plantae')
        self.assertEqual(iconic_group(['Fungi','','','']), 'Fungi')
        self.assertEqual(iconic_group(['Animalia','Mollusca','Gastropoda','']), 'Mollusca')
        for group in ('Aves','Mammalia','Insecta','Amphibia','Arachnida','Actinopterygii'):
            self.assertEqual(iconic_group(['Animalia','Chordata',group,'']),group)
        self.assertEqual(iconic_group(['Animalia','Chordata','Sauropsida','Testudines']),'Reptilia')

    def test_restricted_score_of_one_does_not_become_reliable_evidence(self):
        row={'candidates':[{'sci':'Pinus strobus','p':1.,'global_p':.01}],
             'open_top':[{'sci':'Marmara fasciella','p':.9,'group':'Insecta'}]}
        assessment=assess_visual(row,'Plantae')
        self.assertTrue(assessment['category_conflict'])
        self.assertTrue(assessment['weak_visual_evidence'])
        _,uncertain=core.fuse([core.Detection('image','image','',row['candidates'])])
        self.assertTrue(uncertain)

    def test_open_identification_does_not_invent_a_user_category(self):
        row={'candidates':[{'sci':'Anser anser','p':.99,'global_p':.99}],
             'open_top':[{'sci':'Anser anser','p':.99,'group':'Aves'}]}
        self.assertFalse(assess_visual(row)['category_conflict'])
        self.assertEqual(assess_visual(row)['status'],'supported')

    def test_scoped_result_without_original_score_cannot_be_confident(self):
        row={'candidates':[{'sci':'Pinus strobus','p':1.0,'group':'Plantae'}]}
        self.assertTrue(assess_visual(row,'Plantae')['weak_visual_evidence'])


class ApplicationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session=core.Session('category-policy')
        self.session.expert.settings=Settings(enabled=False)
        self.session.allowed_species=lambda:set()
        self.session.log=Mock()
        self.session.mic=FakePhone()
        self.session.clients=set()
        self.session.broadcast_observation=AsyncMock()
        self.session.flush_pending=AsyncMock()
        self.session.play_feedback=AsyncMock(return_value=False)
        self.session.speak_expert=AsyncMock(return_value={'status':'completed'})
        self.session.inject=AsyncMock()
        self.session.may_speak=lambda:True
        core.SESSIONS[self.session.id]=self.session

    async def asyncTearDown(self):
        core.SESSIONS.pop(self.session.id,None)
        await self.session.close()

    async def capture(self,result,taxa):
        request=self.session.camera_request
        request.update(phase='capturing',confirmed=True)
        image=io.BytesIO();Image.new('RGB',(64,64),'green').save(image,format='JPEG')
        self.session.names.update(taxa)
        async with ASGIClient() as client:
            with patch('server.classify_frame',return_value=result) as classify:
                response=await client.post('/api/frame',params={'session':self.session.id,
                    'obs':request['obs'],'capture':request['id']},content=image.getvalue())
        self.assertEqual(response.status_code,200,response.text)
        return self.session.observations[self.session.focus],classify

    async def test_explicit_plant_target_survives_conflicting_moth_evidence(self):
        await self.session.tool('look',{'target':'new','group':'Plantae'})
        result={'candidates':[{'sci':'Pinus strobus','p':.98,'global_p':.01,'group':'Plantae'}],
                'open_top':[{'sci':'Marmara fasciella','p':.9,'group':'Insecta'}],
                'ms':1,'sharpness':2}
        obs,classify=await self.capture(result,{'Pinus strobus':{'sci':'Pinus strobus','en':'Eastern White Pine','group':'Plantae'}})
        self.assertEqual(classify.call_args.args[3],'Plantae')
        self.assertEqual((obs.target_group,obs.group,obs.identified_group),('Plantae','Plantae','Plantae'))
        self.assertTrue(obs.uncertain)
        self.assertTrue(obs.visual_assessment['category_conflict'])
        self.assertEqual(self.session.task_status_snapshot()[-1]['stage'],'needs_target_confirmation')
        await self.session.answer_vision(obs.id)
        question=self.session.speak_expert.await_args.args[0]
        self.assertIn('plant',question)
        self.assertIn('?',question)
        self.assertNotIn('Looks like',question)
        self.assertFalse(self.session.items)

    async def test_service_ignoring_scope_cannot_publish_a_cross_category_candidate(self):
        await self.session.tool('new_observation',{'group':'Plantae'})
        result={'candidates':[{'sci':'Marmara fasciella','p':.99,'group':'Insecta'}], 'ms':1}
        obs,_=await self.capture(result,{'Marmara fasciella':{'sci':'Marmara fasciella','en':'Moth','group':'Insecta'}})
        self.assertEqual(obs.target_group,'Plantae')
        self.assertEqual(obs.fused,[])
        self.assertTrue(obs.uncertain)
        self.assertTrue(obs.visual_assessment['category_conflict'])

    async def test_result_query_does_not_leave_a_duplicate_automatic_clarification(self):
        await self.session.tool('new_observation',{'group':'Plantae'})
        result={'candidates':[{'sci':'Pinus strobus','p':.98,'global_p':.01,'group':'Plantae'}],
                'open_top':[{'sci':'Marmara fasciella','p':.9,'group':'Insecta'}]}
        obs,_=await self.capture(result,{'Pinus strobus':{'sci':'Pinus strobus','en':'Pine','group':'Plantae'}})
        self.session.turn_question='What is the result?'
        self.session.rt_send=AsyncMock()
        await self.session.finish_route({'status':'completed','output':[{'type':'function_call',
            'name':'get_observation','call_id':'result','arguments':'{}'}]})
        self.assertIn((obs.id,obs.image_revision),self.session.vision_answered)
        await self.session.answer_vision({'obs':obs.id,'image_revision':obs.image_revision,
                                         'request_id':obs.analysis_request_id})
        self.assertEqual(self.session.speak_expert.await_count,1)

    async def test_unknown_or_guessed_group_can_still_be_corrected_by_image(self):
        await self.session.new_focus('Mammalia')  # Old inferred category; no user constraint.
        await self.session.request_look()
        result={'candidates':[{'sci':'Anser anser','p':.99,'global_p':.99,'group':'Aves'}],
                'open_top':[{'sci':'Anser anser','p':.99,'group':'Aves'}], 'ms':1}
        obs,_=await self.capture(result,{'Anser anser':{'sci':'Anser anser','en':'Greylag Goose','group':'Aves'}})
        self.assertIsNone(obs.target_group)
        self.assertEqual(obs.group,'Aves')
        self.assertFalse(obs.uncertain)

    async def test_user_can_correct_category_or_release_it_without_mixing_evidence(self):
        await self.session.tool('new_observation',{'group':'Plantae'})
        old=self.session.observations[self.session.focus]
        await self.session.request_look(group='Insecta')
        current=self.session.observations[self.session.focus]
        self.assertNotEqual(old.id,current.id)
        self.assertEqual(current.target_group,'Insecta')
        self.assertEqual(old.status,'discarded')
        await self.session.request_look(group='Any')
        self.assertIsNone(self.session.observations[self.session.focus].target_group)


if __name__=='__main__':unittest.main()
