"""Offline protocol/dispatch regression tests; no credentials or external services."""
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
        self.events = []
        self.audio = []

    async def send_json(self, event):
        self.events.append(event)

    async def send_bytes(self, pcm):
        self.audio.append(pcm)


class BackendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        s.ITEMS.clear()
        self.phone = FakePhone()
        self.c = s.PhoneConnection(self.phone)
        self.c.rt_send = AsyncMock()
        s.CONNECTIONS.add(self.c)

    async def asyncTearDown(self):
        s.CONNECTIONS.discard(self.c)
        if self.c.playback_task:
            self.c.playback_task.cancel()
            await asyncio.gather(self.c.playback_task, return_exceptions=True)
        self.c.executor.shutdown(wait=True)
        s.ITEMS.clear()

    async def response(self, tool, args):
        await self.c.handle_rt({"type": "response.done", "response": {"id": "r1", "status": "completed", "output": [
            {"type": "function_call", "name": tool, "call_id": "c1", "arguments": json.dumps(args)}]}})

    def sent_types(self):
        return [c.args[0]["type"] for c in self.c.rt_send.call_args_list]

    async def test_hello_contract(self):
        await self.c.handle_phone({"type": "hello", "lang": "zh"})
        self.assertEqual([e["type"] for e in self.phone.events], ["state", "checklist"])
        self.assertEqual(self.c.session()["model"], s.MODEL)
        self.assertEqual(self.c.session()["audio"]["input"]["transcription"]["model"], "higgs-stt-3.1")

    async def test_logging_is_earcon_without_response(self):
        taxon = {"sci": "Cardinalis cardinalis", "en": "Northern Cardinal", "zh": "北美红雀", "group": "Aves"}
        await self.c.handle_rt({"type": "response.created", "response": {"id": "r1"}})
        await self.c.handle_rt({"type": "response.output_audio.delta", "response_id": "r1", "delta": "AAA="})
        with patch.object(s, "resolve", return_value=taxon):
            await self.response("add_observation", {"name": "北美红雀", "group": "Aves", "count": 2, "notes": "bushes"})
        self.assertEqual(s.ITEMS[0]["count"], 2)
        self.assertEqual(self.phone.audio, [], "Logging must suppress model preambles")
        self.assertEqual(self.sent_types(), ["conversation.item.create"])
        self.assertIn({"type": "earcon", "kind": "logged"}, self.phone.events)
        await self.response("set_count", {"name": "北美红雀", "count": 4})
        self.assertEqual(s.ITEMS[0]["count"], 4)
        await self.response("remove_observation", {"name": "Northern Cardinal"})
        self.assertEqual(s.ITEMS, [])
        self.assertNotIn("response.create", self.sent_types())

    async def test_read_tool_continues_response(self):
        await self.response("get_checklist", {})
        self.assertEqual(self.sent_types(), ["conversation.item.create", "response.create"])

    async def test_ignore_does_not_send_anything_upstream(self):
        await self.response("ignore", {})
        self.assertEqual(self.sent_types(), [])
        self.assertFalse(any(e["type"] == "earcon" for e in self.phone.events))

    async def test_manual_edits_and_validation(self):
        s.ITEMS.append({"id": "o1", "count": 2})
        await self.c.handle_phone({"type": "edit", "id": "o1", "count": 3})
        self.assertEqual(s.ITEMS[0]["count"], 3)
        with self.assertRaises(ValueError):
            await self.c.handle_phone({"type": "edit", "id": "o1", "count": -1})
        await self.c.handle_phone({"type": "delete", "id": "o1"})
        self.assertEqual(s.ITEMS, [])

    async def test_barge_in_flushes_and_drops_late_audio(self):
        self.c.response_id = "r1"
        self.c.play_until = time.monotonic() + 5
        await self.c.handle_rt({"type": "input_audio_buffer.speech_started"})
        self.assertIn({"type": "flush"}, self.phone.events)
        self.assertIn("response.cancel", self.sent_types())
        await self.c.handle_rt({"type": "response.output_audio.delta", "response_id": "r1", "delta": base64.b64encode(b"\0\0").decode()})
        self.assertEqual(self.phone.audio, [])

    async def test_pcm_phase_across_odd_sized_packets(self):
        x = np.arange(101, dtype="<i2").tobytes()
        self.c.connected = True
        await self.c.ingest_audio(x[:62])
        await self.c.ingest_audio(x[62:])
        split = self.c.audio_queue.get_nowait() + self.c.audio_queue.get_nowait()
        other = s.PhoneConnection(FakePhone())
        try:
            other.connected = True
            await other.ingest_audio(x)
            self.assertEqual(split, other.audio_queue.get_nowait())
            self.assertEqual(len(split), 102)
        finally:
            other.executor.shutdown(wait=True)

    async def test_birdnet_two_windows_and_checklist_guard(self):
        names = {"sci": "Cardinalis cardinalis", "en": "Northern Cardinal", "zh": "北美红雀"}
        self.c.names[names["sci"]] = names
        self.c.analyze = lambda _: [{"scientific_name": names["sci"], "common_name": names["en"], "confidence": 0.8}]
        await self.c.bird_window(np.zeros(1), 3 * s.SR)
        self.assertEqual(len(self.c.pending), 0)
        await self.c.bird_window(np.zeros(1), int(4.5 * s.SR))
        self.assertEqual(len(self.c.pending), 1)
        self.assertEqual(len(self.c.history), 2)
        await self.c.bird_window(np.zeros(1), 6 * s.SR)
        self.assertEqual(len(self.c.pending), 1)
        self.c.pending.clear()
        self.c.notified.clear()
        s.ITEMS.append({"sci": names["sci"]})
        await self.c.bird_window(np.zeros(1), int(7.5 * s.SR))
        self.assertEqual(len(self.c.pending), 0)

    async def test_realtime_reconnect_reinjects_summary(self):
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
                return json.dumps({"type": "session.created"})
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
        s.ITEMS.append({"count": 2, "en": "Northern Cardinal", "zh": "北美红雀"})
        self.c.rt_send = s.PhoneConnection.rt_send.__get__(self.c)
        real_sleep = asyncio.sleep
        async def fast_sleep(delay):
            await real_sleep(0)
        with patch.object(s, "api_key", return_value="fake-test-key"), \
             patch.object(s.websockets, "connect", side_effect=connect), \
             patch.object(s.asyncio, "sleep", side_effect=fast_sleep):
            await self.c.realtime_loop()
        self.assertEqual(len(sessions), 2)
        for rt in sessions:
            self.assertEqual(rt.sent[0]["session"]["model"], s.MODEL)
            self.assertIn("2 Northern Cardinal", rt.sent[1]["item"]["content"][0]["text"])

    async def test_quiet_mode_suppresses_audio(self):
        self.c.quiet = True
        await self.c.handle_rt({"type": "response.created", "response": {"id": "r1"}})
        await self.c.handle_rt({"type": "response.output_audio.delta", "response_id": "r1", "delta": "AAA="})
        self.assertEqual(self.phone.audio, [])
        self.c.tapped = True
        await self.c.handle_rt({"type": "response.created", "response": {"id": "r2"}})
        await self.c.handle_rt({"type": "response.output_audio.delta", "response_id": "r2", "delta": "AAA="})
        await self.c.handle_rt({"type": "response.done", "response": {"id": "r2", "status": "completed", "output": []}})
        self.assertEqual(len(self.phone.audio), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
