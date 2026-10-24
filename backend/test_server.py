"""Offline routing/session regressions; no credentials or external services."""
import asyncio
import base64
import json
import io
import math
from types import SimpleNamespace
from urllib.parse import urlencode
import time
import unittest
from unittest.mock import AsyncMock, patch
from PIL import Image

import numpy as np
import server as s


class FakePhone:
    def __init__(self):
        self.events, self.audio = [], []

    async def send_json(self, event):
        self.events.append(event)

    async def send_bytes(self, pcm):
        self.audio.append(pcm)


class ASGIClient:
    """Tiny ASGI HTTP harness; avoids adding httpx to the demo venv."""
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def post(self, path, params=None, content=b""):
        messages = []
        async def receive():
            return {"type": "http.request", "body": content, "more_body": False}
        async def send(message):
            messages.append(message)
        await s.app({"type": "http", "http_version": "1.1", "method": "POST" if content else "GET",
                     "scheme": "http", "path": path, "raw_path": path.encode(),
                     "query_string": urlencode(params or {}).encode(), "headers": [],
                     "server": ("test", 80), "client": ("test", 1)}, receive, send)
        body = b"".join(m.get("body", b"") for m in messages)
        return SimpleNamespace(status_code=messages[0]["status"], text=body.decode(errors="replace"),
                               json=lambda: json.loads(body))

    async def get(self, path):
        return await self.post(path)


class BackendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.phone = FakePhone()
        self.c = s.Session("test")
        self.client = s.Client(self.phone, self.c)
        self.c.clients.add(self.client)
        self.c.rt_send = AsyncMock()
        self.c.allowed_species = lambda: set()
        self.c.mic = self.client

    async def asyncTearDown(self):
        await self.c.close()

    def seed(self, sci="Cardinalis cardinalis", count=2, session=None):
        session = session or self.c
        obs = s.Observation("ob" + str(len(session.observations)), status="confirmed", count=count,
                            fused=[{"sci": sci, "en": "Northern Cardinal", "photo": "", "p": 1}])
        session.observations[obs.id] = obs
        return obs

    async def route(self, name, args=None):
        await self.c.finish_route({"status": "completed", "output": [{"type": "function_call",
            "name": name, "call_id": "c1", "arguments": json.dumps(args or {})}]})

    async def test_hello_and_vad(self):
        await self.c.handle_phone({"type": "hello", "lang": "zh"})
        self.assertEqual([e["type"] for e in self.phone.events], ["state", "checklist"])
        vad = self.c.session()["audio"]["input"]["turn_detection"]
        self.assertFalse(vad["create_response"])
        self.assertFalse(vad["interrupt_response"])  # manual barge-in only
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
            self.seed()
            self.assertFalse(other.items)
        finally:
            await other.close()

    async def test_manual_edit_validation(self):
        obs = self.seed()
        await self.c.handle_phone({"type": "edit", "id": obs.id, "count": 3})
        self.assertEqual(self.c.items[0]["count"], 3)
        with self.assertRaises(ValueError):
            await self.c.handle_phone({"type": "edit", "id": obs.id, "count": -1})
        await self.c.handle_phone({"type": "delete", "id": obs.id})
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
        self.assertEqual(self.c.pending[0][0], "text")
        self.assertIn("input_audio_buffer.clear", [c.args[0]["type"] for c in self.c.rt_send.call_args_list])
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
        self.seed()
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
        self.c.mic = None
        self.seed()
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

    async def test_focus_capture_expiry_and_confirmed_retention(self):
        await self.route("look", {"hint": "bill"})
        obs = self.c.observations[self.c.focus]
        self.assertIn({"type": "capture", "obs": obs.id, "n": 3, "hint": "bill"}, self.phone.events)
        await self.route("look")
        self.assertEqual(self.c.focus, obs.id)
        obs.touched -= 121
        await self.c.expire_focus()
        self.assertIsNone(self.c.focus)
        self.assertEqual(obs.status, "discarded")
        new = await self.c.new_focus()
        new.status = "confirmed"
        new.touched -= 121
        await self.c.expire_focus()
        self.assertEqual(new.status, "confirmed")
        self.assertFalse(self.phone.audio)

    async def test_checklist_aggregation_maps_edits_to_observations(self):
        a = self.seed(count=2)
        b = self.seed(count=3)
        self.assertEqual(len(self.c.items), 1)
        self.assertEqual(self.c.items[0]["count"], 5)
        await self.c.handle_phone({"type": "edit", "id": a.id, "count": 4})
        self.assertEqual(self.c.items[0]["count"], 4)
        self.assertEqual(b.status, "discarded")
        await self.c.handle_phone({"type": "delete", "id": a.id})
        self.assertFalse(self.c.items)
        self.assertEqual(a.status, "discarded")

    async def test_sound_window_attached_once_and_range_filtered(self):
        self.c.names["a"] = {"sci": "a", "en": "A"}
        self.c.analyze = lambda _: [{"scientific_name": "a", "common_name": "A", "confidence": .8}]
        await self.c.request_look()
        obs = self.c.observations[self.c.focus]
        await self.c.bird_window(np.zeros(1), 3 * s.SR)
        self.assertEqual(obs.detections[0].kind, "sound")
        await self.c.request_look()
        self.assertEqual(len(obs.detections), 1)
        obs.touched -= 13
        await self.c.bird_window(np.zeros(1), 4 * s.SR)
        self.assertEqual(len(obs.detections), 1)

    async def test_confirm_focus_and_expert_stub(self):
        obs = self.seed()
        obs.status = "open"
        self.c.focus = obs.id
        await self.route("confirm_observation", {"count": 2, "notes": "pair"})
        self.assertEqual(obs.status, "confirmed")
        self.assertEqual(self.c.items[0]["count"], 2)
        self.assertIn({"type": "earcon", "kind": "logged"}, self.phone.events)
        result = await self.c.tool("ask_expert", {"question": "bill?"})
        self.assertEqual(result, {"answer": None, "note": "expert provider not configured"})
        await self.c.handle_phone({"type": "observation_action", "id": obs.id, "action": "discard"})
        self.assertFalse(self.c.items)

    async def test_nonbird_focus_uses_open_set_and_description_needs_name(self):
        self.c.allowed_species = lambda: (_ for _ in ()).throw(AssertionError("bird filter used for nonbird"))
        await self.c.tool("look", {"group": "Insecta"})
        obs = self.c.observations[self.c.focus]
        self.assertEqual(obs.group, "Insecta")
        with patch.object(s, "resolve") as resolve:
            await self.c.tool("describe", {"text": "red with black dots"})
            resolve.assert_not_called()
        self.assertEqual(obs.fused, [])
        with patch.object(s, "resolve", return_value={"sci": "Coccinella septempunctata", "en": "Seven-spot Ladybird", "group": "Insecta"}):
            await self.c.tool("describe", {"text": "seven spot ladybird", "name": "seven spot ladybird"})
        self.assertEqual(obs.fused[0]["sci"], "Coccinella septempunctata")

    async def test_sound_does_not_refresh_idle_focus(self):
        await self.c.request_look()
        obs = self.c.observations[self.c.focus]
        touched = obs.touched
        self.c.names["a"] = {"sci": "a", "en": "A"}
        self.c.analyze = lambda _: [{"scientific_name": "a", "common_name": "A", "confidence": .8}]
        await self.c.bird_window(np.zeros(1), 3 * s.SR)
        self.assertEqual(obs.touched, touched)
        self.assertEqual(len(obs.detections), 1)

    async def test_frame_endpoint_mock_service_and_thumbnail(self):
        obs = await self.c.new_focus()
        obs.awaiting_answer = True
        image = io.BytesIO()
        Image.new("RGB", (64, 64), "red").save(image, format="JPEG")
        s.SESSIONS[self.c.id] = self.c
        self.c.names["a"] = {"sci": "a", "en": "A"}
        try:
            # ASGI transport keeps the session on this event loop.
            async with ASGIClient() as client:
                with patch.object(s.requests, "post") as post:
                    post.return_value.json.return_value = {"candidates": [{"sci": "a", "p": .8}], "sharpness": 5, "ms": 10}
                    response = await client.post("/api/frame", params={"session": self.c.id, "obs": obs.id, "x": 0, "y": 0, "w": .5, "h": .5}, content=image.getvalue())
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(post.call_args.kwargs["json"]["crop"], [0, 0, .5, .5])
                    self.assertIsNone(post.call_args.kwargs["json"]["candidates"])
                detection = response.json()["detection"]
                self.assertEqual((await client.get(detection["thumb"])).status_code, 200)
                self.assertEqual(obs.fused[0]["sci"], "a")
                self.assertEqual(self.c.pending[0][:2], ("vision", obs.id))
                self.assertTrue(any(e["type"] == "observation" and e["detections"] for e in self.phone.events))
                with patch.object(s.requests, "post", side_effect=s.requests.ConnectionError):
                    response = await client.post("/api/frame", params={"session": self.c.id, "obs": obs.id}, content=image.getvalue())
                    self.assertEqual(response.status_code, 503)
                    self.assertIn("Vision service unavailable", response.text)
                response = await client.post("/api/frame", params={"session": self.c.id, "obs": obs.id}, content=b"not jpeg")
                self.assertEqual(response.status_code, 422)
                response = await client.post("/api/frame", params={"session": self.c.id, "obs": obs.id, "x": .9, "y": 0, "w": .5, "h": 1}, content=image.getvalue())
                self.assertEqual(response.status_code, 422)
                obs.status = "confirmed"
                response = await client.post("/api/frame", params={"session": self.c.id, "obs": obs.id}, content=image.getvalue())
                self.assertEqual(response.status_code, 409)
        finally:
            s.SESSIONS.pop(self.c.id, None)

    def test_fusion_synthetic_log_weights_priors_and_uncertainty(self):
        def detection(kind, a, b):
            return s.Detection("d", kind, s.now_iso(), [{"sci": "a", "en": "A", "p": a}, {"sci": "b", "en": "B", "p": b}])
        evidence = [detection("image", .8, .2), detection("sound", .1, .9)]
        fused, uncertain = s.fuse(evidence)
        # .8*sqrt(.1) / (.8*sqrt(.1)+.2*sqrt(.9)) = 4/7
        self.assertAlmostEqual(fused[0]["p"], 4 / 7)
        self.assertTrue(uncertain)
        fused, uncertain = s.fuse(evidence, priors={"a": 2})
        self.assertAlmostEqual(fused[0]["p"], .8)
        self.assertFalse(uncertain)
        fused, _ = s.fuse(evidence, allowed={"b"})
        self.assertEqual(fused[0]["sci"], "b")
        description = s.Detection("d2", "description", s.now_iso(), [{"sci": "b", "en": "B", "p": 1}])
        boosted, _ = s.fuse(evidence + [description])
        self.assertEqual(boosted[0]["sci"], "b")
        empty_description = s.Detection("d3", "description", s.now_iso(), text="red bird")
        self.assertEqual(s.fuse(evidence), s.fuse(evidence + [empty_description]))
        self.assertEqual(s.fuse([]), ([], True))

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
