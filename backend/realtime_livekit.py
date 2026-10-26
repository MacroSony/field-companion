"""Opt-in LiveKit media owner around the existing application/tool controller.

Routing stays on a text-only GPT connection. A separate LiveKit-managed GPT
Realtime session receives speech and speaks approved answers. Neither the
browser nor the voice session executes app tools.
"""
import asyncio
from collections import deque
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import secrets
import time
import uuid

import numpy as np
import requests

from livekit_settings import LiveKitSettings
from realtime_webrtc import openai_config
from tool_feedback import recording
from conversation_state import ScoutGate, SharedDialogue, ConversationServices, MEMORY_RULE
from livekit_diagnostics import VoiceCapture


def browser_token(settings, room_name, identity):
    from livekit import api
    return (api.AccessToken(settings.api_key, settings.api_secret)
        .with_identity(identity).with_name('Field phone').with_ttl(timedelta(minutes=5))
        .with_grants(api.VideoGrants(room_join=True, room=room_name,
            can_publish=True, can_publish_sources=['microphone'], can_subscribe=True,
            can_publish_data=False, can_update_own_metadata=False)).to_jwt())


def worker_request(path, *, samples=None, lat=0, lng=0):
    url = os.environ.get('FIELD_BIRDNET_WORKER_URL', 'http://127.0.0.1:8811')
    from urllib.parse import urlsplit
    parsed = urlsplit(url)
    if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or parsed.path not in ('', '/'):
        raise RuntimeError('BirdNET worker must be on loopback')
    key_file = os.environ.get('FIELD_BIRDNET_WORKER_KEY_FILE', '')
    key = Path(key_file).read_text(encoding='utf-8').strip() if key_file else ''
    if not key:
        raise RuntimeError('BirdNET worker is not configured')
    response = requests.request('POST' if samples is not None else 'GET', url.rstrip('/') + path,
        params={'lat': lat, 'lng': lng}, data=None if samples is None else np.asarray(samples, dtype='<f4').tobytes(),
        headers={'X-Field-Worker': key, 'Content-Type': 'application/octet-stream'}, timeout=(3, 40))
    if response.status_code != 200:
        raise RuntimeError('BirdNET worker unavailable')
    return response.json()


class LiveKitSessionMixin:
    transport = 'livekit'

    def __init__(self, session_id, realtime=None):
        super().__init__(session_id, realtime=realtime or openai_config())
        self.voice = None
        self.voice_task = None
        self.voice_lock = asyncio.Lock()
        self.voice_room = None
        self.voice_settings = None
        self.voice_speaking = False
        self.voice_tail_until = 0.0
        self.voice_context = deque(maxlen=8)
        self.routing_task = None
        self.transcript_guard_task = None
        self.wake = ScoutGate()
        self.conversation_services = ConversationServices(self.realtime)
        self.dialogue = SharedDialogue(self.conversation_services.summarize)
        self.voice_input_lock = asyncio.Lock()
        self.addressing_mode = 'scout'
        self.voice_capture = VoiceCapture(self)

    @property
    def echo_guard(self):
        return False  # Browser AEC + addressed SDK interruption; no 180 ms app gate.

    def state_payload(self):
        result = super().state_payload()
        pending_reply = self.wake.current(self.reply_binding())
        return {**result, 'connected': bool(self.connected and self.voice and self.voice.ready),
                'media_connected': bool(self.voice and self.voice.ready),
                'speaking': self.voice_speaking, 'transport': 'livekit',
                'interruption_owner': 'livekit', 'interruption_mode': 'addressed',
                'wake_name': self.wake.name, 'wake_required': self.addressing_mode == 'scout',
                'reply_without_name': bool(pending_reply),
                'reply_without_name_for': max(0, pending_reply.expires - self.wake.clock()) if pending_reply else 0,
                'memory_recent_messages': len(self.dialogue.recent),
                'memory_has_summary': bool(self.dialogue.summary)}

    def reply_binding(self):
        request = self.camera_request
        return json.dumps({'focus': self.focus,
            'camera': [request['id'], request['phase']] if request else None,
            'suggestions': sorted(self.suggestions),
            'investigations': list(self.investigations.jobs)}, sort_keys=True)

    def heard_assistant(self, text, *, interrupted=False, kind='question'):
        self.dialogue.add('assistant', text, interrupted=interrupted)
        if kind == 'question' and any(self.consent_context().values()):
            kind = 'confirmation'
        self.wake.offer(text, self.reply_binding(), kind=kind, interrupted=interrupted)

    def config(self, phase):
        config = super().config(phase)
        config['instructions'] += '\n' + MEMORY_RULE
        if phase == 'answer' and self.expert_readout is None:
            config['instructions'] += ('\nYour name is Scout. Do not begin spoken replies with your own '
                'wake name. When clarification is needed, ask one specific question ending in a question mark.')
        if phase == 'route':
            config['instructions'] += json.dumps(self.dialogue.snapshot(), ensure_ascii=False)
            if self.addressing_mode == 'ambient' and not getattr(self.turn_question,'addressed',False):
                config['instructions'] += (
                    '\nThis unprefixed turn has not been accepted as a reply to an active question. '
                    'Do not recreate an invitation from historical dialogue. Apply the ambient '
                    'addressing rule: clearly directed commands may run; uncertain nature '
                    'questions use show_silently and other chatter uses ignore.')
            if self.addressing_mode == 'scout' or getattr(self.turn_question, 'addressed', False):
                config['instructions'] += (
                '\nAddressing override: this latest request has ALREADY been accepted by '
                'the application as addressed to Scout (named request, current-question '
                'reply, or deliberate text input). Do not ignore it as chatter. If intent '
                'or target is unclear, call clarify_request with one concise question in '
                'the user\'s language. Never guess an old species or action. Historical '
                'dialogue is not a new request. Latest question being answered, if any: '
                    + json.dumps(getattr(self.turn_question, 'reply_to', None), ensure_ascii=False))
        return config

    @property
    def recording_position(self):
        return self.voice_capture.position if hasattr(self,'voice_capture') else 0

    def extract_turn_audio(self, start_sample, stop_sample, padding=.3):
        return self.voice_capture.extract(start_sample,stop_sample,padding)

    def voice_frame(self, samples):
        self.voice_capture.feed(samples)

    async def start_enrollment(self, profile):
        if self.speaker_service and self.voice and self.voice.ready:
            await self.voice.interrupt()
        await super().start_enrollment(profile)
        if self.enrollment and self.voice_capture.active:
            self.voice_capture.active['enrollment'] = True

    async def route_accepted(self, name, args):
        if not getattr(self.turn_question, 'memory_recorded', False):
            self.dialogue.add('user',str(self.turn_question))
            if hasattr(self.turn_question,'memory_recorded'):
                self.turn_question.memory_recorded = True
        self.wake.clear()

    def assistant_audio_pending(self):
        return self.voice_speaking or super().assistant_audio_pending()

    def playback_pending(self):
        return self.voice_speaking or super().playback_pending()

    def environment_contaminated(self):
        return self.voice_speaking or time.monotonic() < self.voice_tail_until

    def analyze(self, samples):
        return worker_request('/analyze', samples=samples, lat=self.lat, lng=self.lng)['detections']

    def allowed_species(self):
        return set(worker_request('/allowed', lat=self.lat, lng=self.lng)['species'])

    async def mic_attached(self, client):
        if client is not self.mic:
            return
        await self.close_voice()
        if client is not self.mic:
            return
        client.livekit_token = secrets.token_urlsafe(32)
        client.livekit_token_created = time.monotonic()
        await client.send_json({'type': 'livekit_ready', 'token': client.livekit_token,
            'configured': LiveKitSettings.load().configured})

    async def mic_detached(self, client):
        client.livekit_token = ''
        await self.close_voice()

    async def offer(self, token):
        owner = self.mic
        if (owner is None or not getattr(owner, 'livekit_token', '')
                or not secrets.compare_digest(str(token), owner.livekit_token)
                or time.monotonic() - owner.livekit_token_created > 90):
            raise PermissionError('Reconnect the phone before starting LiveKit')
        async with self.voice_lock:
            if self.mic is not owner or not secrets.compare_digest(str(token), owner.livekit_token):
                raise PermissionError('Phone connection changed')
            settings = LiveKitSettings.load()
            settings.validate()
            owner.livekit_token = ''
            room_name = 'field-' + hashlib.sha256(self.id.encode()).hexdigest()[:12] + '-' + uuid.uuid4().hex[:12]
            identity = 'phone-' + uuid.uuid4().hex
            from livekit_voice import LiveKitVoice
            voice = LiveKitVoice(self, settings, self.realtime)
            self.voice, self.voice_room, self.voice_settings = voice, room_name, settings
            self.voice_task = asyncio.create_task(self.open_voice(voice, room_name, identity, owner))
            # Return the phone token before waiting for RoomIO to attach to it.
            return {'url': settings.url, 'token': browser_token(settings, room_name, identity),
                    'room': room_name, 'identity': identity, 'interruption_mode': 'addressed'}

    async def open_voice(self, voice, room_name, identity, owner):
        try:
            await voice.start(room_name, identity)
            if self.mic is not owner or self.voice is not voice:
                await voice.close()
                return
            if self.routing_task is None or self.routing_task.done():
                self.routing_task = asyncio.create_task(self.realtime_loop())
                self.tasks.append(self.routing_task)
            self.log('livekit_voice_connected', model=self.realtime.model, transport='livekit')
            await self.state()
            await self.flush_pending()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.log('livekit_setup_failed', error_type=type(exc).__name__)
            await voice.close()
            await self.error('LiveKit voice could not connect. End and restart after checking project setup.')
            await self.state()

    async def voice_partial(self, text):
        # The media framework still owns playout. Only a recognized vocative can
        # force interruption before the final transcript/relevance check.
        if (self.mic and self.mic_on and self.voice and self.voice.ready
                and not self.enrollment and not (self.voice_capture.active or {}).get('enrollment')
                and self.voice_speaking and self.wake.addressed(text) is not None):
            await self.voice.interrupt()

    async def voice_input(self, text, *, item_id=None):
        async with self.voice_input_lock:
            turn = self.voice_capture.final(text,item_id)
            if turn.get('duplicate'):
                return
            ids = (turn['recording_id'],) if turn.get('recording_id') else ()
            context = self.recording_audio_context.set(ids)
            try:
                await self.accept_voice_input(text, turn)
            finally:
                self.recording_audio_context.reset(context)

    async def accept_voice_input(self, text, turn):
        if not self.mic or self.closed or not self.voice or not self.voice.ready:
            return
        self.awaiting_transcript = False
        if self.transcript_guard_task:
            self.transcript_guard_task.cancel()
            self.transcript_guard_task = None
        if not self.mic_on or not text.strip():
            self.log('input_ignored', reason='mic_off_or_empty')
            self.voice_capture.settle(turn,'input_ignored','mic_off_or_empty')
            await self.flush_pending()
            return
        if self.enrollment or turn.get('enrollment'):
            self.log('input_ignored',reason='enrollment')
            self.voice_capture.settle(turn,'input_ignored','enrollment')
            await self.flush_pending()
            return
        accepted = self.wake.addressed(text)
        addressed = accepted is not None
        ambient = self.addressing_mode == 'ambient'
        if ambient and not addressed and turn.get('overlap'):
            self.log('input_ignored',reason='ambient_playback_overlap')
            self.voice_capture.settle(turn,'input_ignored','ambient_playback_overlap')
            await self.flush_pending()
            return
        reply_to = None
        reply_lease = None
        camera_action = 'none'
        if accepted is None:
            prompt = self.wake.current(self.reply_binding())
            if not prompt:
                if ambient:
                    await self.queue_voice_candidate(text,turn,addressed=False)
                    return
                else:
                    self.log('input_ignored', reason='wake_name_required')
                    self.voice_capture.settle(turn,'input_ignored','wake_name_required')
                    await self.flush_pending()
                    return
            # The exact explicitly invited camera keyword needs no model call.
            keyword = text.strip(' \t\n.,!！。？?').lower()
            if prompt.kind == 'camera' and keyword in ('start', '开始'):
                decision = {'kind': 'answer', 'camera_action': 'start'}
            else:
                binding = self.reply_binding()
                revision = self.input_revision
                started = time.monotonic()
                self.awaiting_transcript = True
                try:
                    decision = await self.conversation_services.classify_reply_with_deadline(prompt, text)
                except Exception as exc:
                    self.log('reply_addressing_unavailable', error_type=type(exc).__name__,
                        text=text[:1000], elapsed_ms=round((time.monotonic() - started) * 1000))
                    # Service failure is not a semantic rejection. Retain the
                    # question, avoid actions/interruption, and provide recovery.
                    if (not self.closed and self.input_revision == revision
                            and self.wake.current(self.reply_binding()) is prompt):
                        await self.phone_json({'type': 'reply_retry', 'message':
                            'I could not check that reply. Say “Scout” and repeat it.'})
                    self.awaiting_transcript = False
                    self.voice_capture.settle(turn,'input_ignored','reply_service_unavailable')
                    await self.flush_pending()
                    return
                finally:
                    self.awaiting_transcript = False
                self.log('reply_addressing', kind=decision['kind'], camera_action=decision['camera_action'],
                         elapsed_ms=round((time.monotonic() - started) * 1000))
                # A delayed classification cannot answer an expired or replaced question.
                if (self.closed or self.input_revision != revision or self.reply_binding() != binding
                        or self.wake.current(binding) is not prompt):
                    self.voice_capture.settle(turn,'input_ignored','stale_reply')
                    return
            if decision['kind'] != 'answer':
                if ambient:
                    await self.queue_voice_candidate(text,turn,addressed=False)
                    return
                self.log('input_ignored', reason='not_answer_to_pending_question')
                self.voice_capture.settle(turn,'input_ignored','not_answer_to_pending_question')
                await self.flush_pending()
                return
            accepted, reply_to = text, prompt.question
            addressed = True
            reply_lease = (prompt.binding, self.input_revision, prompt.expires)
            if prompt.kind == 'camera':
                camera_action = decision['camera_action']
        self.wake.clear()
        # Empty/partial named requests are still addressed: ask, rather than
        # silently discarding them as fillers or treating an old task as intent.
        accepted = accepted or '(Scout was called without a request.)'
        self.log('user_transcript', text=text, transport='livekit')
        self.user_speaking = self.awaiting_transcript = False
        self.speech_stopped_at = time.monotonic()
        await self.phone_json({'type': 'transcript', 'role': 'user', 'text': text, 'final': True})
        if self.assistant_audio_pending():
            await self.cancel_speech()
        if reply_lease and (self.reply_binding() != reply_lease[0]
                or self.input_revision != reply_lease[1] or self.wake.clock() >= reply_lease[2]):
            self.log('input_ignored', reason='reply_context_changed')
            self.voice_capture.settle(turn,'input_ignored','reply_context_changed')
            await self.flush_pending()
            return
        if camera_action in ('start', 'cancel'):
            self.dialogue.add('user', accepted)
            self.input_revision += 1
            self.auto_speech_paused = False
            await self.camera_confirmation('start' if camera_action == 'start' else 'cancel')
            self.voice_capture.settle(turn,'camera_confirmation')
            await self.flush_pending()
        else:
            await self.handle_phone({'type': 'text', 'text': accepted,
                '_addressed': True, '_reply_to': reply_to, '_voice_input':True,
                '_recording_ids':self.recording_audio_context.get()}, self.mic)
            if (turn.get('recording_id') not in self.recording_turns and not any(
                    row[0]=='text' and turn.get('recording_id') in getattr(row[1],'recording_ids',()) for row in self.pending)):
                self.voice_capture.settle(turn,'direct_action')

    async def queue_voice_candidate(self, text, turn, *, addressed):
        self.log('user_transcript',text=text,transport='livekit',addressed=addressed)
        self.user_speaking = self.awaiting_transcript = False
        await self.phone_json({'type':'transcript','role':'user','text':text,'final':True})
        await self.handle_phone({'type':'text','text':text,'_voice_input':True,
            '_addressed':addressed,'_defer_interrupt':not addressed,
            '_recording_ids':self.recording_audio_context.get()},self.mic)

    async def handle_phone(self, event, client=None):
        if event.get('type') == 'text' and isinstance(event.get('text'), str) and 0 < len(event['text'].strip()) <= 8000:
            addressed = bool(event.get('_addressed')) if event.get('_voice_input') else True
            if addressed:
                self.wake.clear()
                self.dialogue.add('user', event['text'])
            event = {**event, '_addressed':addressed, '_memory_recorded':addressed}
        if event.get('type') == 'addressing_mode':
            self.wake.clear()
        return await super().handle_phone(event, client)

    async def voice_answer(self, text, *, interrupted=False, epoch=None):
        if self.closed or (epoch is not None and epoch != self.epoch):
            return
        self.last_assistant_text = text
        if self.recorder:
            for turn_id in self.recording_turns:
                self.recorder.update(turn_id,assistant_audio=True)
        self.heard_assistant(text, interrupted=interrupted)
        self.log('assistant_transcript', text=text, transport='livekit')
        await self.phone_json({'type': 'transcript', 'role': 'assistant', 'text': text, 'final': True})
        if self.connected and self.rt is not None:
            item_id = 'item_' + uuid.uuid4().hex[:24]
            self.own_item(item_id)
            await super().rt_send({'type': 'conversation.item.create', 'item': {
                'id': item_id, 'type': 'message', 'role': 'assistant',
                'content': [{'type': 'output_text', 'text': text}]}})
        await self.state()

    async def voice_state(self, state):
        was_speaking = self.voice_speaking
        self.voice_speaking = state == 'speaking'
        if was_speaking and not self.voice_speaking:
            self.voice_tail_until = time.monotonic() + .5
        self.log('livekit_agent_state', state=state)
        await self.state()
        if not self.voice_speaking:
            await self.flush_pending()

    async def voice_user_state(self, state):
        if state == 'speaking':
            self.voice_capture.begin()
        else:
            self.voice_capture.end()
        self.user_speaking = state == 'speaking'
        if self.user_speaking:
            # Notification coordination only; addressing decides interruption.
            self.awaiting_transcript = True
            if self.transcript_guard_task:
                self.transcript_guard_task.cancel()
            self.transcript_guard_task = asyncio.create_task(self.transcript_timeout())
        self.log('livekit_user_state', state=state)
        await self.state()
        if not self.user_speaking:
            await self.flush_pending()

    async def transcript_timeout(self):
        await asyncio.sleep(25)
        self.transcript_guard_task = None
        if not self.closed and not self.user_speaking:
            self.awaiting_transcript = False
            self.log('livekit_transcript_wait_expired')
            await self.flush_pending()

    async def voice_false_interruption(self):
        if not self.user_speaking:
            self.awaiting_transcript = False
            if self.transcript_guard_task:
                self.transcript_guard_task.cancel()
                self.transcript_guard_task = None
            await self.flush_pending()

    async def voice_failed(self):
        if self.closed:
            return
        if self.voice:
            self.voice.ready = False
        self.voice_speaking = self.user_speaking = self.awaiting_transcript = False
        await self.error('LiveKit voice disconnected. End and start a new session.')
        await self.state()

    async def rt_send(self, event):
        item = event.get('item', {})
        if event.get('type') == 'conversation.item.create' and item.get('type') == 'function_call_output':
            self.voice_context.append({'tool_result': item.get('output', '')[:16000]})
        await super().rt_send(event)

    async def inject(self, text):
        self.voice_context.append({'backend_context': text[:16000]})
        return await super().inject(text)

    async def generate(self, phase, *, guard=None):
        if phase != 'answer':
            self.voice_context.clear()
            return await super().generate(phase, guard=guard)
        if self.auto_speech_paused or (guard and not guard()) or not self.may_speak():
            return {'status': 'deferred', 'output': []}
        if not self.voice or not self.voice.ready:
            raise ConnectionError('LiveKit voice is not ready')
        self.phase = 'answer'
        context = json.dumps({'user_request': str(self.turn_question),
            'shared_dialogue': self.dialogue.snapshot(),
            'authoritative_backend_results': list(self.voice_context)}, ensure_ascii=False)
        return await self.voice.reply(self.config('answer')['instructions'], context)

    async def play_feedback(self, kind, *, language=None, guard=None):
        if (not self.voice or not self.voice.ready or not self.may_speak() or self.user_speaking
                or self.voice_speaking or self.auto_speech_paused or (guard and not guard())):
            return False
        language = language or ('zh' if self.chinese(self.turn_question) or self.lang.startswith('zh') else 'en')
        text, pcm = recording(kind, language)
        played = await self.voice.say(text, pcm)
        if played and (guard is None or guard()):
            if self.recorder:
                for turn_id in self.recording_audio_context.get():
                    self.recorder.update(turn_id,assistant_audio=True)
            self.last_assistant_text = text
            if kind != 'received':
                self.heard_assistant(text, kind='camera' if kind == 'camera-ready' else 'question')
                # The cached clarification explicitly invites another utterance.
                if kind == 'clarify':
                    self.wake.offer(text + '?', self.reply_binding())
            await self.phone_json({'type': 'transcript', 'role': 'assistant', 'text': text, 'final': True})
            self.log('feedback_played', kind=kind, transport='livekit')
            await self.state()
        return played

    async def phone_audio(self, pcm, *, guard=None):
        # No old PCM speech copy can reach a LiveKit phone.
        return

    async def cancel_speech(self):
        if self.voice:
            await self.voice.interrupt()
        if self.phase != 'answer':
            await super().cancel_speech()
        else:
            self.audio_allowed = False
            await super().flush_audio()
            await self.state()

    async def flush_audio(self):
        if self.voice:
            await self.voice.interrupt()
        await super().flush_audio()

    def start(self):
        self.turn_task = asyncio.create_task(self.turn_worker())
        self.tasks = [self.turn_task,
                      asyncio.create_task(self.focus_watch()), asyncio.create_task(self.suggestion_loop())]

    async def reset(self):
        self.wake.clear()
        if self.voice:
            await self.voice.interrupt()
            # Recreate the voice model to discard its audio/transcript context too.
            if self.voice.session:
                from livekit.agents import llm
                await self.voice.agent.update_chat_ctx(llm.ChatContext.empty())
        self.voice_context.clear()
        await super().reset()
        self.wake.clear()
        await self.dialogue.reset()
        self.voice_capture.clear()
        await self.state()

    async def close_voice(self):
        self.cancel_enrollment()
        if self.recorder:
            self.recorder.flush('microphone_detached',self.recording_position)
        self.wake.clear()
        if self.transcript_guard_task:
            self.transcript_guard_task.cancel()
            await asyncio.gather(self.transcript_guard_task, return_exceptions=True)
            self.transcript_guard_task = None
        voice, self.voice = self.voice, None
        task, self.voice_task = self.voice_task, None
        room_name, self.voice_room = self.voice_room, None
        settings, self.voice_settings = self.voice_settings, None
        if task and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if voice:
            await voice.close()
        self.voice_capture.clear()
        if self.routing_task:
            self.routing_task.cancel()
            await asyncio.gather(self.routing_task, return_exceptions=True)
            if self.routing_task in self.tasks:
                self.tasks.remove(self.routing_task)
            self.routing_task = None
        self.voice_speaking = self.user_speaking = self.awaiting_transcript = False
        if room_name and settings:
            try:
                from livekit import api
                async with asyncio.timeout(5):
                    async with api.LiveKitAPI(settings.url, settings.api_key, settings.api_secret) as service:
                        await service.room.delete_room(api.DeleteRoomRequest(room=room_name))
            except Exception as exc:
                self.log('livekit_room_cleanup_failed', error_type=type(exc).__name__)

    async def close(self):
        await self.close_voice()
        await self.dialogue.close()
        await self.conversation_services.close()
        await super().close()


def make_session(base, session_id):
    return type('LiveKitSession', (LiveKitSessionMixin, base), {})(session_id)
