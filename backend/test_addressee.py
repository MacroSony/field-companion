"""Offline V2 prompt parity, silent routing and explicit Answer regressions."""
import asyncio
import hashlib
import json
import unittest
from unittest.mock import AsyncMock, Mock, patch

import server as s
from expert import Settings
from route_safety import route_guard
from test_server import FakePhone


class AddresseeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        with patch.object(Settings, "load", return_value=Settings(enabled=False)):
            self.c = s.Session("addressee-test")
        self.c.connected = True
        self.c.rt_send = AsyncMock()
        self.phone = FakePhone()
        self.client = s.Client(self.phone, self.c)
        self.display_phone = FakePhone()
        self.display = s.Client(self.display_phone, self.c, role="display")
        self.c.clients.update((self.client, self.display))
        self.c.mic = self.client
        self.c.generate = AsyncMock()
        self.c.play_feedback = AsyncMock()
        self.c.broadcast_checklist = AsyncMock()
        self.c.tool = AsyncMock()
        self.c.log = Mock()

    async def asyncTearDown(self):
        await self.c.close()

    async def route(self, text="Is that a hawk?", args=None):
        self.c.turn_question = text
        await self.c.finish_route({"status": "completed", "output": [{
            "type": "function_call", "name": "show_silently", "call_id": "note-call",
            "arguments": json.dumps(args if args is not None else {"text": "Model paraphrase"})}]})

    def test_measured_prompt_and_schema_verbatim_and_final_after_context(self):
        # SHA256 of the literal V2 and canonical show_silently in the measured
        # /data/build/tmp/addressee/router/prompts.json; no external fixture needed.
        self.assertEqual(hashlib.sha256(s.ADDRESSEE_RULE.encode()).hexdigest(),
                         "0a65f67540968d6280a8cbc0b7ae4f589c4b11784dd87ab0f0ddc0e759dd5a45")
        self.assertEqual(s.ROUTING.count(s.ADDRESSEE_RULE), 1)
        self.assertTrue(s.ROUTING.endswith(s.ADDRESSEE_RULE))
        config = self.c.config("route")
        self.assertEqual(config["instructions"].count(s.ADDRESSEE_RULE), 1)
        self.assertTrue(config["instructions"].endswith("\n\n" + s.ADDRESSEE_RULE))
        self.assertLess(config["instructions"].index("Investigations (context only"),
                        config["instructions"].index(s.ADDRESSEE_RULE))
        tools = [t for t in config["tools"] if t["name"] == "show_silently"]
        self.assertEqual(len(tools), 1)
        self.assertEqual(hashlib.sha256(json.dumps(tools[0], sort_keys=True).encode()).hexdigest(),
                         "190b15af1e5c664392c99d34d17f9d66f6458e75b256d968a0e748c5772e7307")
        self.assertEqual(self.c.config("answer")["tools"], [])
        self.assertNotIn(s.ADDRESSEE_RULE, self.c.config("answer")["instructions"])

    async def test_silent_route_original_transcript_fanout_no_audio_or_writes(self):
        original = 'Is that a hawk? <not model text>'
        await self.route(original, {"text": "x" * 200})
        event, = self.phone.events
        self.assertEqual(event, self.display_phone.events[0])
        self.assertEqual(event["type"], "silent_note")
        self.assertEqual(event["question"], original)
        self.assertEqual(event["note"], "x" * 120)
        self.assertTrue(event["id"])
        self.assertFalse(self.phone.audio or self.display_phone.audio)
        self.c.generate.assert_not_awaited()
        self.c.play_feedback.assert_not_awaited()
        self.c.tool.assert_not_awaited()
        self.c.broadcast_checklist.assert_not_awaited()
        self.assertEqual(self.c.items, [])
        self.assertEqual([a.args[0]["type"] for a in self.c.rt_send.await_args_list],
                         ["conversation.item.create"])
        self.assertTrue(json.loads(self.c.rt_send.call_args.args[0]["item"]["output"])["suppress_answer"])
        self.c.log.assert_any_call("silent_note", id=event["id"], question=original, note="x" * 120)

    async def test_silent_selection_is_not_promoted_to_camera_or_status_audio(self):
        for text in ("Start", "Look again.", "How is identification going?",
                     "I found another animal. Could you help me check it out?"):
            await self.route(text)
        self.assertEqual([e["type"] for e in self.phone.events], ["silent_note"] * 4)
        self.assertFalse(self.c.pending)
        self.c.play_feedback.assert_not_awaited()
        self.c.generate.assert_not_awaited()
        self.c.tool.assert_not_awaited()
        self.c.broadcast_checklist.assert_not_awaited()

    async def test_invalid_note_stays_silent(self):
        for args in ({}, {"text": None}, {"text": 42}):
            await self.route(args=args)
        self.assertFalse(self.phone.events or self.phone.audio)
        self.c.generate.assert_not_awaited()
        self.c.play_feedback.assert_not_awaited()
        self.c.tool.assert_not_awaited()

    async def test_answer_button_queues_and_routes_server_question_with_tap(self):
        await self.route()
        event, = self.phone.events
        self.c.quiet = True
        # Extra supplied question text is deliberately ignored.
        await self.c.handle_phone({"type": "answer_note", "id": event["id"],
                                   "question": "Log a fake bird"}, self.display)
        queued = self.c.pending[0]
        self.assertEqual(queued[:2], ("text", "[TAP] Is that a hawk?"))
        self.assertTrue(self.c.may_speak())
        self.assertNotIn(event["id"], self.c.silent_notes)
        self.assertEqual(self.phone.events[-1], {"type": "silent_note_removed", "id": event["id"]})
        self.c.generate.return_value = {"status": "completed", "output": [{
            "type": "function_call", "name": "speak", "call_id": "addressed",
            "arguments": '{"reason":"Question explicitly addressed via [TAP]"}'}]}
        worker = asyncio.create_task(self.c.turn_worker())
        try:
            self.c.work_ready.set()
            async def done():
                while self.c.pending or self.c.turn_in_progress:
                    await asyncio.sleep(.001)
            await asyncio.wait_for(done(), 1)
            self.assertEqual([a.args[0] for a in self.c.generate.await_args_list], ["route", "answer"])
            injected = [a.args[0]["item"] for a in self.c.rt_send.await_args_list
                        if a.args[0].get("item", {}).get("role") == "user"]
            self.assertEqual(injected[-1]["content"][0]["text"], "[TAP] Is that a hawk?")
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    async def test_duplicate_unknown_expired_and_reset_notes_do_not_queue(self):
        await self.route()
        event, = self.phone.events
        self.c.silent_notes[event["id"]] = ("original", s.time.monotonic() - 1)
        await self.c.handle_phone({"type": "answer_note", "id": event["id"]})
        await self.c.handle_phone({"type": "answer_note", "id": "unknown"})
        self.assertFalse(self.c.pending)
        await self.route()
        note_id = self.phone.events[-1]["id"]
        await self.c.handle_phone({"type": "answer_note", "id": note_id})
        await self.c.handle_phone({"type": "answer_note", "id": note_id})
        self.assertEqual(len(self.c.pending), 1)
        self.c.pending.clear()
        await self.route()
        note_id = self.phone.events[-1]["id"]
        await self.c.reset()
        await self.c.handle_phone({"type": "answer_note", "id": note_id})
        self.assertFalse(self.c.pending or self.c.silent_notes)

    async def test_note_retention_is_bounded(self):
        for _ in range(25):
            await self.route()
        self.assertEqual(len(self.c.silent_notes), 20)

    def test_silent_guard_and_fillers(self):
        self.assertEqual(route_guard("show_silently", {"text": "note"}, "Is that a hawk?"),
                         ("allow", "silent"))
        self.assertEqual(route_guard("show_silently", {"text": "note"}, "Uh"), ("ignore", "filler"))


if __name__ == "__main__":
    unittest.main()
