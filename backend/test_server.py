"""Offline routing/session regressions; no credentials or external services."""
import asyncio
import base64
import json
import time
import unittest
from unittest.mock import AsyncMock, patch

import numpy as np
import server as s


class FakePhone:
    def __init__(self):
        self.events, self.audio = [], []

    async def send_json(self, event):
        self.events.append(event)

    async def send_bytes(self, pcm):
        self.audio.append(pcm)


class BackendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.phone = FakePhone()
        self.c = s.Session("test")
        self.client = s.Client(self.phone, self.c)
        self.c.clients.add(self.client)
        self.c.rt_send = AsyncMock()

    async def asyncTearDown(self):
        await self.c.close()

    async def route(self, name, args=None):
        await self.c.finish_route({"status": "completed", "output": [{"type": "function_call",
            "name": name, "call_id": "c1", "arguments": json.dumps(args or {})}]})

    async def test_hello_and_vad(self):
        await self.c.handle_phone({"type": "hello", "lang": "zh"})
        self.assertEqual([e["type"] for e in self.phone.events], ["state", "checklist"])
        vad = self.c.session()["audio"]["input"]["turn_detection"]
        self.assertFalse(vad["create_response"])
        self.assertTrue(vad["interrupt_response"])
        self.assertEqual(self.c.config("route")["tool_choice"], "required")
        self.assertEqual(self.c.config("answer")["tools"], [])

    async def test_logging_is_silent_with_shared_checklist(self):
        display = FakePhone()
        self.c.clients.add(s.Client(display, self.c, "display"))
        taxon = {"sci": "Cardinalis cardinalis", "en": "Northern Cardinal", "group": "Aves", "photo": "photo"}
        await self.c.handle_rt({"type": "response.output_audio.delta", "delta": "AAA="})
        with patch.object(s, "resolve", return_value=taxon):
            await self.route("add_observation", {"name": "cardinal", "group": "Aves", "count": 2})
        self.assertEqual(self.phone.audio, [])
        self.assertEqual(self.c.items[0]["count"], 2)
        self.assertIn("lat", self.c.items[0])
        self.assertEqual(self.phone.events, display.events)
        self.assertIn({"type": "earcon", "kind": "logged"}, self.phone.events)
        self.assertEqual([c.args[0]["type"] for c in self.c.rt_send.call_args_list], ["conversation.item.create"])
        await self.route("set_count", {"name": "Northern Cardinal", "count": 4})
        self.assertEqual(self.c.items[0]["count"], 4)
        await self.route("remove_observation", {"name": "Northern Cardinal"})
        self.assertFalse(self.c.items)

    async def test_ignore_no_speech_or_followup(self):
        await self.route("ignore")
        self.assertFalse(self.phone.events)
        self.assertFalse(self.phone.audio)
        self.assertEqual(self.c.rt_send.call_args.args[0]["item"]["type"], "function_call_output")

    async def test_read_and_speak_answer_only_when_allowed(self):
        self.c.generate = AsyncMock()
        for name in ("speak", "get_checklist", "recent_sounds"):
            await self.route(name)
        self.assertEqual(self.c.generate.await_count, 3)
        self.c.quiet = True
        await self.route("speak")
        self.assertEqual(self.c.generate.await_count, 3)
        self.c.tap_at = time.monotonic() - 11
        await self.route("get_checklist")
        self.assertEqual(self.c.generate.await_count, 3)
        self.c.tap_at = time.monotonic()
        await self.route("speak")
        self.assertEqual(self.c.generate.await_count, 4)

    async def test_answer_streams_before_response_done(self):
        self.c.phase = "answer"
        self.c.audio_allowed = True
        await self.c.handle_rt({"type": "response.output_audio.delta", "delta": "AAA="})
        self.assertEqual(self.phone.audio, [b"\0\0"])

    async def test_tap_cancels_and_drops_late_audio(self):
        self.c.phase = "answer"
        self.c.connected = self.c.active = True
        self.c.response_id = "r1"
        self.c.play_until = time.monotonic() + 5
        await self.c.handle_phone({"type": "tap"})
        self.assertIn({"type": "flush"}, self.phone.events)
        self.assertIn("response.cancel", [c.args[0]["type"] for c in self.c.rt_send.call_args_list])
        await self.c.handle_rt({"type": "response.output_audio.delta", "response_id": "r1", "delta": "AAA="})
        self.assertFalse(self.phone.audio)

    async def test_echo_guard_blocks_realtime_not_birdnet(self):
        display = FakePhone()
        self.c.clients.add(s.Client(display, self.c, "display", True))
        self.c.connected = True
        self.c.play_until = time.monotonic() + 1
        await self.c.ingest_audio(bytes(4800))
        self.assertTrue(self.c.state_payload()["echo_guard"])
        self.assertTrue(self.c.audio_queue.empty())
        self.assertEqual(self.c.position, 2400)
        self.c.play_until = time.monotonic() - .4
        await self.c.ingest_audio(bytes(4800))
        self.assertFalse(self.c.audio_queue.empty())

    async def test_audio_fanout_and_session_isolation(self):
        silent, loud = FakePhone(), FakePhone()
        self.c.clients.update([s.Client(silent, self.c, "display"), s.Client(loud, self.c, "display", True)])
        await self.c.phone_audio(b"pcm")
        self.assertEqual(self.phone.audio, loud.audio)
        self.assertFalse(silent.audio)
        other = s.Session("other")
        try:
            self.c.items.append({"id": "one"})
            self.assertFalse(other.items)
        finally:
            await other.close()

    async def test_manual_edit_validation(self):
        self.c.items.append({"id": "o1", "count": 2})
        await self.c.handle_phone({"type": "edit", "id": "o1", "count": 3})
        self.assertEqual(self.c.items[0]["count"], 3)
        with self.assertRaises(ValueError):
            await self.c.handle_phone({"type": "edit", "id": "o1", "count": -1})
        await self.c.handle_phone({"type": "delete", "id": "o1"})
        self.assertFalse(self.c.items)

    async def test_pcm_phase(self):
        self.c.connected = True
        x = np.arange(101, dtype="<i2").tobytes()
        await self.c.ingest_audio(x[:62])
        await self.c.ingest_audio(x[62:])
        split = self.c.audio_queue.get_nowait() + self.c.audio_queue.get_nowait()
        other = s.Session("other")
        try:
            other.connected = True
            await other.ingest_audio(x)
            self.assertEqual(split, other.audio_queue.get_nowait())
        finally:
            await other.close()

    async def test_birdnet_threshold_and_metadata(self):
        names = {"sci": "Cardinalis cardinalis", "en": "Northern Cardinal", "photo": "photo"}
        self.c.names[names["sci"]] = names
        self.c.analyze = lambda _: [{"scientific_name": names["sci"], "common_name": names["en"], "confidence": .8}]
        await self.c.bird_window(np.zeros(1), 3 * s.SR)
        self.assertFalse(self.c.pending)
        await self.c.bird_window(np.zeros(1), int(4.5 * s.SR))
        self.assertEqual(len(self.c.pending), 1)
        self.assertEqual(len(self.c.history), 2)
        heard = next(e for e in self.phone.events if e["type"] == "heard")
        self.assertTrue(all(k in heard for k in ("en", "sci", "photo", "lat", "lng")))

    async def test_update_waits_for_matching_echo(self):
        config = self.c.config("answer")
        task = asyncio.create_task(self.c.update_session(config))
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        await self.c.handle_rt({"type": "session.updated", "session": config})
        await task

    async def test_transcription_enqueues_route(self):
        await self.c.handle_rt({"type": "input_audio_buffer.speech_stopped"})
        await self.c.handle_rt({"type": "conversation.item.input_audio_transcription.completed", "transcript": "two birds"})
        self.assertEqual(self.c.pending[0][0], "audio")
        self.assertIsNotNone(self.c.pending[0][2])

    async def test_realtime_reconnect_reinjects_checklist(self):
        class FakeRealtime:
            def __init__(inner):
                inner.sent = []
            async def __aenter__(inner):
                return inner
            async def __aexit__(inner, *args):
                return False
            async def send(inner, raw):
                inner.sent.append(json.loads(raw))
            async def recv(inner):
                return json.dumps({"type": "session.created", "session": self.c.session()})
            def __aiter__(inner):
                return inner
            async def __anext__(inner):
                raise StopAsyncIteration
        sessions = []
        def connect(url, **kwargs):
            self.assertIn("?model=" + s.MODEL, url)
            rt = FakeRealtime()
            sessions.append(rt)
            if len(sessions) == 2:
                self.c.closed = True
            return rt
        self.c.items.append({"count": 2, "en": "Northern Cardinal"})
        self.c.rt_send = s.Session.rt_send.__get__(self.c)
        real_sleep = asyncio.sleep
        async def fast_sleep(delay):
            await real_sleep(0)
        with patch.object(s, "api_key", return_value="offline-placeholder"), \
             patch.object(s.websockets, "connect", side_effect=connect), \
             patch.object(s.asyncio, "sleep", side_effect=fast_sleep):
            await self.c.realtime_loop()
        self.assertEqual(len(sessions), 2)
        for rt in sessions:
            self.assertEqual(rt.sent[0]["session"]["model"], s.MODEL)
            self.assertIn("2 Northern Cardinal", rt.sent[1]["item"]["content"][0]["text"])

    async def test_new_mic_replaces_old_and_retains_session(self):
        class Socket(FakePhone):
            def __init__(self):
                super().__init__()
                self.queue = asyncio.Queue()
                self.closed = False
            async def receive(self):
                return await self.queue.get()
            async def close(self, **kwargs):
                self.closed = True
                await self.queue.put({"type": "websocket.disconnect"})
        self.c.clients.clear()
        self.c.items.append({"id": "retained", "count": 2})
        old, new = Socket(), Socket()
        old_client, new_client = s.Client(old, self.c), s.Client(new, self.c)
        old_task = asyncio.create_task(old_client.run())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        new_task = asyncio.create_task(new_client.run())
        for _ in range(10):
            await asyncio.sleep(0)
        self.assertTrue(old.closed)
        self.assertIs(self.c.mic, new_client)
        self.assertIsNone(self.c.expiry_task)
        self.assertNotIn(old_client, self.c.clients)
        self.assertTrue(any(e.get("items") == self.c.items for e in new.events))
        await new.close()
        await asyncio.gather(old_task, new_task)
        self.assertFalse(self.c.closed)
        self.assertIsNotNone(self.c.expiry_task)
        self.c.expiry_task.cancel()
        await asyncio.gather(self.c.expiry_task, return_exceptions=True)

    async def test_session_expires_after_last_client(self):
        s.SESSIONS[self.c.id] = self.c
        try:
            with patch.object(s, "SESSION_TTL", .001):
                await self.c.expire()  # connected sessions must not expire
                self.assertFalse(self.c.closed)
                self.c.clients.clear()
                await self.c.expire()
            self.assertTrue(self.c.closed)
            self.assertNotIn(self.c.id, s.SESSIONS)
        finally:
            s.SESSIONS.pop(self.c.id, None)

    async def test_update_serialization(self):
        route, answer = self.c.config("route"), self.c.config("answer")
        first = asyncio.create_task(self.c.update_session(route))
        second = asyncio.create_task(self.c.update_session(answer))
        await asyncio.sleep(0)
        self.assertEqual(self.c.rt_send.await_count, 1)
        await self.c.handle_rt({"type": "session.updated", "session": route})
        await first
        await asyncio.sleep(0)
        self.assertEqual(self.c.rt_send.await_count, 2)
        self.assertFalse(second.done())
        await self.c.handle_rt({"type": "session.updated", "session": answer})
        await second

    def test_english_name_fallback(self):
        taxon = {"id": 42, "name": "Test bird", "preferred_common_name": "中文", "default_photo": {"square_url": "square"}}
        with patch.object(s, "inat", return_value={"results": [{"preferred_common_name": "English Bird"}]}) as lookup:
            names = s.taxon_names(taxon)
        self.assertEqual(names["en"], "English Bird")
        self.assertEqual(names["zh"], "中文")
        self.assertEqual(names["photo"], "square")
        lookup.assert_called_once_with("taxa/42", locale="en")


if __name__ == "__main__":
    unittest.main(verbosity=2)
