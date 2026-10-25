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

    @property
    def echo_guard(self):
        return False  # LiveKit owns validated interruption; no 180 ms app gate.

    def state_payload(self):
        result = super().state_payload()
        return {**result, 'connected': bool(self.connected and self.voice and self.voice.ready),
                'media_connected': bool(self.voice and self.voice.ready),
                'speaking': self.voice_speaking, 'transport': 'livekit',
                'interruption_owner': 'livekit', 'interruption_mode': 'adaptive'}

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
                    'room': room_name, 'identity': identity, 'interruption_mode': 'adaptive'}

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

    async def voice_input(self, text):
        if not self.mic or self.closed or not self.voice or not self.voice.ready:
            return
        self.awaiting_transcript = False
        if self.transcript_guard_task:
            self.transcript_guard_task.cancel()
            self.transcript_guard_task = None
        ignored = self.ignored_input_reason(text) if text.strip() else 'empty'
        if ignored:
            self.log('input_ignored', reason=ignored)
            await self.flush_pending()
            return
        self.log('user_transcript', text=text, transport='livekit')
        self.user_speaking = self.awaiting_transcript = False
        self.speech_stopped_at = time.monotonic()
        await self.phone_json({'type': 'transcript', 'role': 'user', 'text': text, 'final': True})
        await self.handle_phone({'type': 'text', 'text': text}, self.mic)

    async def voice_answer(self, text):
        if self.closed:
            return
        self.last_assistant_text = text
        self.log('assistant_transcript', text=text, transport='livekit')
        await self.phone_json({'type': 'transcript', 'role': 'assistant', 'text': text, 'final': True})
        if self.connected and self.rt is not None:
            item_id = 'item_' + uuid.uuid4().hex[:24]
            self.own_item(item_id)
            await super().rt_send({'type': 'conversation.item.create', 'item': {
                'id': item_id, 'type': 'message', 'role': 'assistant',
                'content': [{'type': 'output_text', 'text': text}]}})

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
        self.user_speaking = state == 'speaking'
        if self.user_speaking:
            # Notification coordination only; LiveKit decides interruption.
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
            'authoritative_backend_results': list(self.voice_context)}, ensure_ascii=False)
        return await self.voice.reply(self.config('answer')['instructions'], context)

    async def play_feedback(self, kind, *, language=None, guard=None):
        if (not self.voice or not self.voice.ready or not self.may_speak() or self.user_speaking
                or self.voice_speaking or self.auto_speech_paused or (guard and not guard())):
            return False
        language = language or ('zh' if self.chinese(self.turn_question) or self.lang.startswith('zh') else 'en')
        text, pcm = recording(kind, language)
        played = await self.voice.say(text, pcm)
        if played:
            self.last_assistant_text = text
            await self.phone_json({'type': 'transcript', 'role': 'assistant', 'text': text, 'final': True})
            self.log('feedback_played', kind=kind, transport='livekit')
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
        if self.voice:
            await self.voice.interrupt()
            # Recreate the voice model to discard its audio/transcript context too.
            if self.voice.session:
                from livekit.agents import llm
                await self.voice.agent.update_chat_ctx(llm.ChatContext.empty())
        self.voice_context.clear()
        await super().reset()

    async def close_voice(self):
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
        await super().close()


def make_session(base, session_id):
    return type('LiveKitSession', (LiveKitSessionMixin, base), {})(session_id)
