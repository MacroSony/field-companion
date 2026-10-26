"""Real SDK contract and playback tests. Run in .venv-livekit; no network/API."""
import asyncio
import contextvars
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

try:
    from livekit import rtc
    from livekit.agents import AgentSession, StopResponse, utils
    from livekit.agents.voice import io
    from livekit_voice import LiveKitVoice, RoutedVoiceAgent, create_session
    from realtime_livekit import browser_token
    from livekit_settings import LiveKitSettings
    SDK = True
except ImportError:
    SDK = False


if SDK:
    class MemorySink(io.AudioOutput):
        def __init__(self):
            super().__init__(label='offline-sink',capabilities=io.AudioOutputCapabilities(pause=True),sample_rate=24000)
            self.frames=0
            self.duration=0
            self.cleared=0
            self.started=asyncio.Event()

        async def capture_frame(self,frame):
            await super().capture_frame(frame)
            self.frames+=1
            self.duration+=frame.duration
            self.started.set()
            await asyncio.sleep(frame.duration)

        def flush(self):
            super().flush()
            if self._pending_playback_count:
                self.on_playback_finished(playback_position=self.duration,interrupted=False)
            self.duration=0

        def clear_buffer(self):
            self.cleared+=1
            super().flush()
            if self._pending_playback_count:
                self.on_playback_finished(playback_position=self.duration,interrupted=True)
            self.duration=0


@unittest.skipUnless(SDK,'Optional SDK is installed in .venv-livekit')
class SDKTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_session_start_does_not_treat_agent_handoff_as_a_transcript(self):
        async with utils.http_context.open():
            session=AgentSession(llm=None,vad=None,user_away_timeout=None,
                turn_handling={'turn_detection':'manual','interruption':{'mode':'vad'}})
            owner=SimpleNamespace(log=lambda *a,**k:None,voice_state=AsyncMock(),
                                  voice_answer=AsyncMock(),voice_failed=AsyncMock())
            voice=LiveKitVoice(owner,None,None,adaptive=False)
            voice.session=session
            voice.bind_session_events()
            with self.assertNoLogs('livekit',level='ERROR'):
                await session.start(RoutedVoiceAgent(),session_host=False,record=False)
                voice.closed=True
                await session.aclose()
            owner.voice_answer.assert_not_awaited()
            await voice.close()

    async def test_realtime_auto_turn_detection_is_off_and_adaptive_recovery_is_explicit(self):
        config=SimpleNamespace(openai_key=lambda:'offline-test-key',model='gpt-realtime-2.1',voice='marin',transcription_model='gpt-transcribe')
        session=create_session(config)
        self.assertFalse(session.llm.capabilities.turn_detection)
        self.assertEqual(session.interruption_detection,'adaptive')
        self.assertEqual(session.options.turn_handling['interruption']['resume_false_interruption'],True)
        await session.aclose()

    async def test_user_hook_cannot_start_a_second_tool_or_answer_loop(self):
        with self.assertRaises(StopResponse):
            await RoutedVoiceAgent().on_user_turn_completed(None,None)

    async def test_wake_mode_preserves_input_but_disables_automatic_interruption(self):
        config=SimpleNamespace(openai_key=lambda:'offline-test-key',model='gpt-realtime-2.1',voice='marin',transcription_model='gpt-transcribe')
        session=create_session(config,wake_required=True)
        self.assertFalse(session.llm.capabilities.turn_detection)
        options=session.options.turn_handling['interruption']
        self.assertFalse(options['enabled'])
        self.assertFalse(options['discard_audio_if_uninterruptible'])
        await session.aclose()

    async def test_phone_jwt_is_short_lived_room_scoped_and_microphone_only(self):
        import jwt
        token=browser_token(LiveKitSettings('wss://example.livekit.cloud','test-key','s'*40),'room-test','phone-test')
        claims=jwt.decode(token,options={'verify_signature':False})
        self.assertEqual(claims['sub'],'phone-test')
        self.assertEqual(claims['video']['room'],'room-test')
        self.assertEqual(claims['video']['canPublishSources'],['microphone'])
        self.assertFalse(claims['video']['canPublishData'])
        self.assertLessEqual(claims['exp']-claims['nbf'],300)

    async def test_room_capability_is_single_use_and_token_returns_before_agent_ready(self):
        import time
        import server as core
        from realtime_livekit import make_session
        from test_server import FakePhone
        session=make_session(core.Session,'single-use-test')
        client=core.Client(FakePhone(),session)
        session.mic=client
        client.livekit_token='private-capability'
        client.livekit_token_created=time.monotonic()
        settings=LiveKitSettings('wss://example.livekit.cloud','test-key','s'*40)
        session.open_voice=AsyncMock()
        try:
            with patch.object(LiveKitSettings,'load',return_value=settings):
                connection=await session.offer('private-capability')
                self.assertTrue(connection['token'])
                self.assertFalse(session.voice.ready)
                with self.assertRaises(PermissionError):
                    await session.offer('private-capability')
        finally:
            session.voice_room=None  # No real Cloud room was created.
            await session.close()

    async def test_cached_audio_is_played_and_interrupted_by_real_agent_session(self):
        async with utils.http_context.open():
            session=AgentSession(llm=None,vad=None,user_away_timeout=None,
                turn_handling={'turn_detection':'manual','interruption':{'mode':'vad'}})
            sink=MemorySink()
            session.output.audio=sink
            await session.start(RoutedVoiceAgent(),session_host=False,record=False)
            owner=SimpleNamespace(log=lambda *a,**k:None)
            voice=LiveKitVoice(owner,None,None,adaptive=False)
            voice.session=session
            voice.context=contextvars.copy_context()
            voice.ready=True
            try:
                played=await asyncio.wait_for(voice.say('Test cue.',bytes(4800)),5)
                self.assertTrue(played)
                self.assertGreater(sink.frames,0)
                sink.started.clear()
                playing=asyncio.create_task(voice.say('Long test cue.',bytes(48000)))
                await asyncio.wait_for(sink.started.wait(),5)
                await voice.interrupt()
                self.assertFalse(await asyncio.wait_for(playing,5))
                self.assertGreater(sink.cleared,0)
            finally:
                await session.aclose()


if __name__=='__main__':
    unittest.main()
