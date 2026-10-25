"""Offline provider boundaries and Realtime handshake/playback regressions."""
import asyncio
from dataclasses import asdict
import json
import os
import unittest
from unittest.mock import AsyncMock, patch

import realtime_provider as rp
import server as s


def openai_config():
    return rp.RealtimeConfig(provider="openai", model="gpt-realtime-2.1",
                             transcription_model="gpt-transcribe", voice="marin")


class ConfigurationTests(unittest.TestCase):
    def test_default_remains_higgs_without_openai_settings(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(rp, "local_values", return_value={}):
            config = rp.RealtimeConfig.load()
        self.assertEqual(config.provider, "higgs")
        self.assertEqual(config.model, s.MODEL)
        self.assertIn("api.boson.ai", config.url)

    def test_process_choice_overrides_local_without_mutating_environment(self):
        local = {"REALTIME_PROVIDER": "higgs", "OPENAI_REALTIME_MODEL": "local-model",
                 "OPENAI_API_KEY": "offline-placeholder"}
        with patch.dict(os.environ, {"REALTIME_PROVIDER": "openai", "OPENAI_REALTIME_MODEL": "process-model"}, clear=True), \
                patch.object(rp, "local_values", return_value=local):
            before = dict(os.environ)
            config = rp.RealtimeConfig.load()
            self.assertEqual(config.model, "process-model")
            self.assertEqual(config.openai_key(), "offline-placeholder")
            self.assertEqual(dict(os.environ), before)
            self.assertNotIn("offline-placeholder", repr(config))
            self.assertNotIn("offline-placeholder", json.dumps(asdict(config)))

    def test_unknown_provider_is_rejected_not_silently_changed(self):
        with patch.dict(os.environ, {"REALTIME_PROVIDER": "other"}, clear=True), \
                patch.object(rp, "local_values", return_value={}):
            with self.assertRaises(ValueError):
                rp.RealtimeConfig.load()

    def test_missing_openai_key_does_not_fall_back_to_boson(self):
        with patch.dict(os.environ, {"BOSON_API_KEY": "offline-boson"}, clear=True), \
                patch.object(rp, "local_values", return_value={}):
            with self.assertRaisesRegex(RuntimeError, "OPENAI_API_KEY"):
                openai_config().openai_key()

    def test_model_is_url_encoded_without_changing_host(self):
        config = rp.RealtimeConfig(provider="openai", model="a&host=other")
        self.assertEqual(config.url, "wss://api.openai.com/v1/realtime?model=a%26host%3Dother")


class OpenAIProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.c = s.Session("openai-offline", realtime=openai_config())
        self.c.phone_json = AsyncMock()
        self.c.phone_audio = AsyncMock()

    async def asyncTearDown(self):
        await self.c.close()

    async def test_ga_session_and_each_phase_preserve_serial_routing(self):
        config = self.c.session()
        self.assertNotIn("model", config)
        self.assertEqual(config["type"], "realtime")
        for side in ("input", "output"):
            self.assertEqual(config["audio"][side]["format"], {"type": "audio/pcm", "rate": 24000})
        self.assertEqual(config["audio"]["input"]["transcription"]["model"], "gpt-transcribe")
        self.assertFalse(config["audio"]["input"]["turn_detection"]["create_response"])
        self.assertFalse(config["audio"]["input"]["turn_detection"]["interrupt_response"])
        self.assertIsNone(config["audio"]["input"]["noise_reduction"])
        self.assertEqual(config["audio"]["output"]["voice"], "marin")
        for phase, modalities, choice in (("route", ["text"], "required"), ("answer", ["audio"], "none")):
            update = self.c.config(phase)
            self.assertEqual(update["type"], "realtime")
            self.assertEqual(update["output_modalities"], modalities)
            self.assertEqual(update["tool_choice"], choice)
            self.assertFalse(update["parallel_tool_calls"])
            self.assertEqual(bool(update["tools"]), phase == "route")
        self.assertEqual(self.c.state_payload()["model"], "gpt-realtime-2.1")
        self.assertEqual(self.c.state_payload()["provider"], "openai")

    async def test_default_created_is_not_configuration_ack(self):
        expected = self.c.session()
        self.assertFalse(self.c.realtime.accepts_initial({"type": "session.created", "session": expected}, expected))
        self.assertTrue(self.c.realtime.accepts_initial({"type": "session.updated", "session": expected}, expected))
        for field, bad in (("tool_choice", "auto"), ("output_modalities", ["audio"]), ("instructions", "stale")):
            wrong = {**expected, field: bad}
            with self.assertRaises(RuntimeError):
                self.c.realtime.accepts_initial({"type": "session.updated", "session": wrong}, expected)

    async def test_wrong_audio_format_is_not_accepted(self):
        expected = self.c.session()
        wrong = json.loads(json.dumps(expected))
        wrong["audio"]["output"]["format"]["rate"] = 48000
        with self.assertRaisesRegex(RuntimeError, "24 kHz"):
            self.c.realtime.accepts_initial({"type": "session.updated", "session": wrong}, expected)

    async def test_initial_auto_reply_or_auto_interrupt_is_rejected(self):
        expected = self.c.session()
        for flag in ("create_response", "interrupt_response"):
            wrong = json.loads(json.dumps(expected))
            wrong["audio"]["input"]["turn_detection"][flag] = True
            with self.assertRaisesRegex(RuntimeError, "control"):
                self.c.realtime.accepts_initial({"type": "session.updated", "session": wrong}, expected)

    async def test_transcription_and_voice_must_be_acknowledged(self):
        expected = self.c.session()
        for side, field, bad in (("input", "transcription", {"model": "wrong"}), ("output", "voice", "wrong")):
            wrong = json.loads(json.dumps(expected))
            wrong["audio"][side][field] = bad
            with self.assertRaises(RuntimeError):
                self.c.realtime.accepts_initial({"type": "session.updated", "session": wrong}, expected)

    async def test_real_loop_waits_for_updated_before_connecting_or_seeding(self):
        c = self.c
        expected = c.session()
        stages = []

        class Socket:
            def __init__(self):
                self.received = 0
                self.sent = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def send(self, raw):
                self.sent.append(json.loads(raw))

            async def recv(self):
                stages.append((c.connected, len(self.sent)))
                self.received += 1
                kind = "session.created" if self.received == 1 else "session.updated"
                return json.dumps({"type": kind, "session": expected})

            def __aiter__(self):
                return self

            async def __anext__(self):
                self.finished_connected = c.connected
                c.closed = True
                raise StopAsyncIteration

        socket = Socket()
        real_sleep = asyncio.sleep

        async def no_delay(_):
            await real_sleep(0)

        with patch.object(c, "realtime_key", return_value="offline-placeholder"), \
                patch.object(s.websockets, "connect", return_value=socket) as connect, \
                patch.object(s.asyncio, "sleep", side_effect=no_delay):
            await c.realtime_loop()
        self.assertEqual(stages, [(False, 1), (False, 1)])
        self.assertTrue(socket.finished_connected)
        self.assertIn("api.openai.com/v1/realtime", connect.call_args.args[0])
        self.assertEqual(socket.sent[1]["type"], "conversation.item.create")
        self.assertIn("Current confirmed checklist", socket.sent[1]["item"]["content"][0]["text"])

    async def test_error_message_redacts_active_provider_key(self):
        with patch.object(self.c, "realtime_key", return_value="offline-placeholder"), \
                patch.object(self.c, "log") as log:
            await self.c.handle_rt({"type": "error", "error": {"code": "invalid_request", "message": "bad offline-placeholder"}})
        self.assertNotIn("offline-placeholder", str(log.call_args_list))
        self.assertIn("[redacted]", str(log.call_args_list))

    async def test_openai_inactive_cancel_releases_barrier(self):
        future = asyncio.get_running_loop().create_future()
        self.c.cancel_barrier = {"future": future}
        with patch.object(self.c, "realtime_key", return_value="offline-placeholder"):
            await self.c.handle_rt({"type": "error", "error": {"code": "response_cancel_not_active"}})
        self.assertTrue(future.done())

    async def test_unsolicited_route_audio_is_never_played(self):
        self.c.phase = "route"
        self.c.audio_allowed = True
        await self.c.handle_rt({"type": "response.output_audio.delta", "delta": "AAAAAA=="})
        self.c.phone_audio.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
