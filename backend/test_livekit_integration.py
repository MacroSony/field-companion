"""Merged recording/speaker/mode behavior on native LiveKit voice frames."""
import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import wave

import numpy as np
import server as core
from realtime_livekit import make_session
from test_server import FakePhone


class NativeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.session = make_session(core.Session,'native-integration')
        self.session.recording_directory = Path(self.tmp.name)/'recordings'
        self.session.rt = SimpleNamespace(send=AsyncMock())
        self.session.connected = True
        self.phone = FakePhone()
        self.client = core.Client(self.phone,self.session)
        self.session.clients.add(self.client)
        self.session.mic = self.client
        self.voice = SimpleNamespace(ready=True,interrupt=AsyncMock(),say=AsyncMock(return_value=True),
            reply=AsyncMock(return_value={'status':'completed'}),close=AsyncMock())
        self.session.voice = self.voice
        self.session.allowed_species = lambda:set()

    async def asyncTearDown(self):
        await self.session.close()
        self.tmp.cleanup()

    def feed(self, seconds, value=.03):
        self.session.voice_frame(np.full(round(48000*seconds),value,dtype=np.float32))

    def rows(self):
        path=self.session.recording_directory/'turns.jsonl'
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()] if path.exists() else []

    async def route(self,name,args=None):
        _,value,_=self.session.pending.popleft()
        self.session.turn_question=value
        self.session.turn_context=value.context
        self.session.turn_revision=self.session.input_revision
        self.session.recording_turns=value.recording_ids
        await self.session.finish_route({'status':'completed','output':[
            {'type':'function_call','call_id':'integration','name':name,'arguments':json.dumps(args or {})}]})
        if self.session.recorder:
            for turn_id in value.recording_ids:
                self.session.recorder.settle(turn_id)
        return value

    async def test_native_recording_and_shadow_scores_use_voice_not_the_bird_ring(self):
        self.session.speaker_service='http://local-speaker'
        await self.session.set_recording(True)
        self.session.recorder.on_audio=self.session.score_turn
        self.session.ring.fill(.8)
        self.session.position=987654
        captured=[]
        def score(url,action,samples,profile=None):
            captured.append(samples.copy())
            return {'scores':{'owner':-.7},'ms':1}
        with patch('server.speaker_request',side_effect=score):
            self.feed(1,.01)
            await self.session.voice_user_state('speaking')
            self.feed(2,.03)
            await self.session.voice_user_state('listening')
            await self.session.voice_input('Scout, how many birds?',item_id='native-turn-1')
            await self.route('get_checklist')
            await self.session.voice_answer('There are no birds logged.')
            self.feed(.4,.01)
            await asyncio.gather(*self.session.speaker_tasks)
        rows=self.rows()
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['routed_tool'],'get_checklist')
        self.assertEqual(rows[0]['item_id'],'native-turn-1')
        self.assertEqual(rows[0]['speaker_scores'],{'owner':-.7})
        self.assertTrue(rows[0]['assistant_audio'])
        self.assertEqual(rows[0]['alignment'],'livekit_vad_native_voice_position')
        self.assertLess(float(np.max(captured[0])),.04)
        with wave.open(str(self.session.recording_directory/rows[0]['wav_filename']),'rb') as wav:
            self.assertEqual(wav.getframerate(),48000)
            self.assertGreater(wav.getnframes(),2*48000)
        self.assertTrue(self.voice.reply.await_count)

    async def test_recording_opt_in_never_saves_old_audio_and_off_stops_new_files(self):
        self.feed(3,.8)
        await self.session.set_recording(True)
        await self.session.voice_user_state('speaking')
        self.feed(1,.03)
        await self.session.voice_user_state('listening')
        await self.session.voice_input('unaddressed chatter',item_id='recorded')
        self.feed(.4)
        rows=self.rows()
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['reason'],'wake_name_required')
        with wave.open(str(self.session.recording_directory/rows[0]['wav_filename']),'rb') as wav:
            samples=np.frombuffer(wav.readframes(wav.getnframes()),dtype='<i2')/32768
        self.assertLess(float(samples.max()),.04)
        await self.session.set_recording(False)
        await self.session.voice_user_state('speaking')
        self.feed(1,.7)
        await self.session.voice_user_state('listening')
        await self.session.voice_input('more chatter',item_id='not-recorded')
        self.feed(.4)
        self.assertEqual(len(self.rows()),1)
        self.assertFalse(np.any(self.session.voice_capture.ring))

    async def test_enrollment_reads_twenty_seconds_of_native_voice_and_suppresses_late_final(self):
        self.session.speaker_service='http://local-speaker'
        self.session.position=1000000
        requests=[]
        def speaker(url,action,samples,profile=None):
            requests.append((action,len(samples),profile,float(np.max(samples))))
            return {'ok':True,'scores':{},'ms':1}
        with patch('server.speaker_request',side_effect=speaker):
            await self.session.start_enrollment('owner')
            await self.session.voice_user_state('speaking')
            self.feed(20,.02)
            await self.session.voice_partial('Scout, log two mallards')
            await self.session.stop_enrollment()
            await self.session.voice_user_state('listening')
            await self.session.voice_input('Scout, log two mallards.',item_id='late-enrollment-final')
            await asyncio.gather(*self.session.speaker_tasks)
        self.assertEqual(requests,[('enroll',20*48000,'owner',requests[0][3])])
        self.assertAlmostEqual(requests[0][3],.02,places=5)
        self.assertFalse(self.session.pending)
        self.assertFalse(self.session.items)
        self.assertTrue(any(e.get('type')=='enrollment' and e.get('status')=='ok' for e in self.phone.events))

    async def test_ambient_uncertain_question_is_silent_until_answer_button(self):
        await self.session.handle_phone({'type':'addressing_mode','value':'ambient'},self.client)
        self.voice.interrupt.reset_mock()
        await self.session.voice_input('Is that a hawk?',item_id='ambient-question')
        value=await self.route('show_silently',{'text':'A model paraphrase'})
        self.assertFalse(value.addressed)
        self.voice.reply.assert_not_awaited()
        self.voice.say.assert_not_awaited()
        self.voice.interrupt.assert_not_awaited()
        self.assertFalse(self.session.dialogue.recent)
        note=next(e for e in self.phone.events if e.get('type')=='silent_note')
        self.assertEqual(note['question'],'Is that a hawk?')
        await self.session.handle_phone({'type':'answer_note','id':note['id']},self.client)
        self.assertTrue(self.session.pending[-1][1].addressed)
        self.session.turn_question=self.session.pending[-1][1]
        self.assertIn('ALREADY been accepted',self.session.config('route')['instructions'])
        self.assertFalse(any(e.get('type')=='earcon' for e in self.phone.events))

    async def test_scout_named_request_cannot_be_downgraded_to_a_silent_card(self):
        await self.session.voice_input('Scout, can you help me?',item_id='explicit')
        await self.route('show_silently',{'text':'Possible question'})
        self.assertFalse(self.session.silent_notes)
        self.voice.reply.assert_awaited_once()

    async def test_mode_switch_drops_old_implicit_requests_but_preserves_explicit_ones(self):
        await self.session.handle_phone({'type':'addressing_mode','value':'ambient'},self.client)
        await self.session.voice_input('Is that a hawk?',item_id='implicit')
        await self.session.voice_input('Scout, how many birds?',item_id='explicit')
        await self.session.handle_phone({'type':'addressing_mode','value':'scout'},self.client)
        self.assertEqual(len(self.session.pending),1)
        self.assertTrue(self.session.pending[0][1].addressed)
        self.assertTrue(self.session.state_payload()['wake_required'])

    async def test_unprefixed_ambient_audio_cannot_interrupt_playback(self):
        self.session.addressing_mode='ambient'
        self.session.voice_speaking=True
        await self.session.voice_user_state('speaking')
        await self.session.voice_user_state('listening')
        await self.session.voice_input('Assistant echo or bystander',item_id='overlap')
        self.assertFalse(self.session.pending)
        self.voice.interrupt.assert_not_awaited()

    async def test_duplicate_native_final_is_recorded_and_routed_once(self):
        await self.session.voice_input('Scout, how many birds?',item_id='one')
        await self.session.voice_input('Scout, how many birds?',item_id='one')
        self.assertEqual(len(self.session.pending),1)
        self.assertEqual(len(self.session.dialogue.recent),1)

    async def test_unconfigured_enrollment_does_not_interrupt_existing_speech(self):
        await self.session.start_enrollment('owner')
        self.voice.interrupt.assert_not_awaited()
        self.assertTrue(any(e.get('type')=='enrollment' and e.get('status')=='error' for e in self.phone.events))
