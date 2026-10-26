"""LiveKit owns speech playout/VAD; accepted addressing authorizes interruption.

The existing text-only router continues to own app tools. This avoids two
independent tool loops and preserves route safety, silent logging and jobs.
"""
import asyncio
import contextvars
import numpy as np

from livekit import api, rtc
from livekit.agents import Agent, AgentSession, StopResponse, llm, room_io, utils
from livekit.plugins import openai, silero
from openai.types.realtime import AudioTranscription, RealtimeReasoning


class RoutedVoiceAgent(Agent):
    def __init__(self):
        super().__init__(instructions=(
            'You are the voice of Field Companion. The application decides which turns '
            'need a spoken answer. Speak only when explicitly asked by the application. '
            'Use only the authoritative backend results supplied for that answer. '
            'Never identify a species from a guess.'))

    async def on_user_turn_completed(self, turn_ctx, new_message):
        # With client-side realtime turn-taking the SDK commits accepted audio
        # before this hook. Its final transcription is forwarded by the event
        # handler below. The application's existing router decides what to do.
        raise StopResponse()


def create_session(config, *, adaptive=True, model=None, vad=None, wake_required=False):
    realtime = model or openai.realtime.RealtimeModel(
        api_key=config.openai_key(), model=config.model, voice=config.voice,
        turn_detection=None, modalities=['audio'], reasoning=RealtimeReasoning(effort='low'),
        input_audio_transcription=AudioTranscription(model=config.transcription_model,
            prompt='The birdwatching assistant is named Scout. Users may address it as Scout.'),
        input_audio_noise_reduction=None)
    if model is None:
        realtime.update_options(max_response_output_tokens=1024)
    return AgentSession(
        llm=realtime, vad=vad or silero.VAD.load(), user_away_timeout=None,
        video_sampler=None,
        turn_handling={
            'turn_detection': 'vad',
            'endpointing': {'min_delay': .5, 'max_delay': 3.0},
            'interruption': {'enabled': not wake_required,
                             'discard_audio_if_uninterruptible': not wake_required,
                             'mode': 'adaptive' if adaptive else 'vad',
                             'min_duration': .5, 'false_interruption_timeout': 2.0,
                             'resume_false_interruption': True},
            'preemptive_generation': {'enabled': False}},
    )


class LiveKitVoice:
    def __init__(self, owner, settings, config, *, adaptive=True):
        self.owner, self.settings, self.config = owner, settings, config
        self.adaptive = adaptive
        self.room = None
        self.session = None
        self.closed = False
        self.ready = False
        self.tasks = set()
        self.handle = None
        self.stop_event = asyncio.Event()
        self.runner = None
        self.answer_epoch = None
        self.capture_track = None
        self.capture_task = None

    def task(self, coroutine):
        if self.closed:
            coroutine.close()
            return
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        def finished(done):
            self.tasks.discard(done)
            if not done.cancelled() and done.exception():
                self.owner.log('livekit_callback_failed', error_type=type(done.exception()).__name__)
        task.add_done_callback(finished)
        return task

    async def start(self, room_name, phone_identity):
        ready = asyncio.get_running_loop().create_future()
        self.runner = asyncio.create_task(self.run(room_name, phone_identity, ready))
        await asyncio.wait_for(asyncio.shield(ready), 45)

    async def run(self, room_name, phone_identity, ready):
        async with utils.http_context.open():
            try:
                self.context = contextvars.copy_context()
                await self.connect(room_name, phone_identity)
                if not ready.done():
                    ready.set_result(True)
                await self.stop_event.wait()
            except asyncio.CancelledError:
                if not ready.done():
                    ready.cancel()
                raise
            except Exception as exc:
                if not ready.done():
                    ready.set_exception(ConnectionError('LiveKit voice setup failed (' + type(exc).__name__ + ')'))
                elif not self.closed:
                    await self.owner.voice_failed()
            finally:
                self.ready = False
                if self.session:
                    await self.session.aclose()
                if self.room:
                    await self.room.disconnect()

    async def connect(self, room_name, phone_identity):
        self.settings.bind_inference_environment()
        self.room = rtc.Room()
        @self.room.on('track_subscribed')
        def diagnostic_track(track, publication, participant):
            if track.kind == rtc.TrackKind.KIND_AUDIO and participant.identity == phone_identity:
                if track is self.capture_track:
                    return
                if self.capture_task:
                    self.capture_task.cancel()
                self.capture_track = track
                self.capture_task = self.task(self.capture_voice(track))
        token = (api.AccessToken(self.settings.api_key, self.settings.api_secret)
            .with_identity('agent-' + room_name).with_name('Field Companion')
            .with_grants(api.VideoGrants(room_join=True, room=room_name,
                                        can_publish=True, can_subscribe=True,
                                        can_publish_data=True, can_update_own_metadata=False)).to_jwt())
        await self.room.connect(self.settings.url, token)
        self.session = create_session(self.config, adaptive=self.adaptive, wake_required=True)
        self.bind_session_events()

        self.agent = RoutedVoiceAgent()
        await self.session.start(self.agent, room=self.room, record=False,
            session_host=False, room_options=room_io.RoomOptions(
                participant_identity=phone_identity, text_input=False, video_input=False,
                audio_input=room_io.AudioInputOptions(pre_connect_audio=False),
                audio_output=room_io.AudioOutputOptions(), text_output=False,
                close_on_disconnect=True))
        self.ready = True

    def bind_session_events(self):
        @self.session.on('user_input_transcribed')
        def transcription(event):
            if event.is_final:
                self.task(self.owner.voice_input(event.transcript, item_id=event.item_id))
            elif hasattr(self.owner, 'voice_partial'):
                self.task(self.owner.voice_partial(event.transcript))

        @self.session.on('conversation_item_added')
        def conversation(event):
            if isinstance(event.item, llm.ChatMessage) and event.item.role == 'assistant' and event.item.text_content:
                self.owner.log('livekit_assistant_turn', interrupted=event.item.interrupted)
                self.task(self.owner.voice_answer(event.item.text_content, interrupted=event.item.interrupted,
                    epoch=self.answer_epoch))

        @self.session.on('agent_state_changed')
        def state(event):
            self.task(self.owner.voice_state(event.new_state))

        @self.session.on('user_state_changed')
        def user_state(event):
            self.task(self.owner.voice_user_state(event.new_state))

        @self.session.on('agent_false_interruption')
        def false_interruption(event):
            self.owner.log('livekit_false_interruption', resumed=event.resumed)
            self.task(self.owner.voice_false_interruption())

        @self.session.on('error')
        def error(event):
            self.owner.log('livekit_voice_error', error_type=type(event.error).__name__)
            if not getattr(event.error, 'recoverable', False):
                self.task(self.owner.voice_failed())

        @self.session.on('close')
        def closed(event):
            if not self.closed:
                self.task(self.owner.voice_failed())

    async def reply(self, instructions, context):
        if not self.ready or self.closed:
            raise ConnectionError('LiveKit voice is not connected')
        # Raw ASR includes ignored bystander speech. Rebuild from application
        # memory before every approved answer, so both GPT connections see the
        # same accepted conversation rather than separate hidden histories.
        await self.agent.update_chat_ctx(llm.ChatContext.empty())
        self.answer_epoch = getattr(self.owner, 'epoch', None)
        self.handle = self.context.run(self.session.generate_reply, user_input=context,
            instructions=instructions, tool_choice='none')
        handle = self.handle
        await handle
        if handle.exception():
            raise ConnectionError('LiveKit voice generation failed')
        self.owner.log('livekit_reply_finished', interrupted=handle.interrupted)
        return {'status': 'cancelled' if handle.interrupted else 'completed', 'output': []}

    async def capture_voice(self, track):
        stream = rtc.AudioStream(track,sample_rate=48000,num_channels=1)
        try:
            async for packet in stream:
                samples = np.frombuffer(packet.frame.data,dtype='<i2').astype(np.float32)/32768
                self.owner.voice_frame(samples)
        finally:
            await stream.aclose()

    async def say(self, text, pcm):
        if not self.ready or self.closed:
            return False
        async def frames():
            for offset in range(0, len(pcm), 960):
                data = pcm[offset:offset + 960]
                if data:
                    yield rtc.AudioFrame(data, sample_rate=24000, num_channels=1,
                                         samples_per_channel=len(data) // 2)
        handle = self.context.run(self.session.say, text, audio=frames(), add_to_chat_ctx=False)
        self.handle = handle
        await handle
        return not handle.interrupted and handle.exception() is None

    async def interrupt(self):
        if self.ready and not self.closed:
            await self.session.interrupt(force=True)

    async def close(self):
        if self.closed:
            return
        self.closed, self.ready = True, False
        self.stop_event.set()
        if self.runner and self.runner is not asyncio.current_task():
            self.runner.cancel()
            await asyncio.gather(self.runner, return_exceptions=True)
        for task in list(self.tasks):
            if task is not asyncio.current_task():
                task.cancel()
        await asyncio.gather(*(t for t in self.tasks if t is not asyncio.current_task()), return_exceptions=True)
