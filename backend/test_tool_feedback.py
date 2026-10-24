"""Offline ordering, cancellation and speech-policy tests for pre-tool feedback."""
import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import server as s
from expert import NaturalistExpert, Settings
from tool_feedback import CLIPS, recording


class FeedbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.events = []
        self.phone = SimpleNamespace(
            send_json=AsyncMock(side_effect=lambda e: self.events.append(e)),
            send_bytes=AsyncMock(side_effect=lambda pcm: self.events.append({"type": "pcm", "bytes": len(pcm)})))
        self.c = s.Session("feedback-test")
        self.c.expert = NaturalistExpert(Settings(enabled=False))
        self.c.connected = True
        self.c.rt_send = AsyncMock()
        self.client = s.Client(self.phone, self.c)
        self.client.playback_protocol = 1
        self.c.clients.add(self.client)
        self.c.mic = self.client

    async def asyncTearDown(self):
        await self.c.close()

    async def test_feedback_arrives_before_slow_tool_finishes_without_model_request(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def tool(name, args):
            self.events.append({"type": "tool_started"})
            started.set()
            await release.wait()
            return {"ok": True, "waiting_for_frames": True}
        self.c.tool = tool
        response = {"status": "completed", "output": [{"type": "function_call", "name": "look",
                    "call_id": "look-1", "arguments": "{}"}]}
        work = asyncio.create_task(self.c.finish_route(response))
        try:
            await asyncio.wait_for(started.wait(), 1)
            self.assertFalse(work.done())
            types = [e["type"] for e in self.events]
            self.assertLess(types.index("audio_start"), types.index("pcm"))
            self.assertLess(types.index("pcm"), types.index("tool_started"))
            self.assertLess(types.index("audio_end"), types.index("tool_started"))
            self.assertTrue(self.client.playback_pending)
            self.assertEqual(next(e["text"] for e in self.events if e["type"] == "transcript"), "Let me take a look.")
            self.c.rt_send.assert_not_awaited()
        finally:
            release.set()
            await work

    async def test_chinese_feedback_uses_question_language_and_is_echo_filtered(self):
        self.c.turn_question = "这是什么？"
        await self.c.acknowledge_tool("look")
        self.assertTrue(any(e.get("text") == "好的，我看一下。" for e in self.events))
        self.assertTrue(self.c.transcript_is_echo("好的，我看一下。"))
        self.assertFalse(self.c.transcript_is_echo("好的。"))
        self.assertFalse(self.c.transcript_is_echo("停一下。"))

    async def test_quiet_speech_pending_playback_and_fast_actions_do_not_get_extra_voice(self):
        for field, value in [("quiet", True), ("user_speaking", True),
                             ("awaiting_transcript", True), ("auto_speech_paused", True),
                             ("connected", False), ("play_until", s.time.monotonic() + 10)]:
            original = getattr(self.c, field)
            setattr(self.c, field, value)
            await self.c.acknowledge_tool("look")
            setattr(self.c, field, original)
        self.client.playback_pending = True
        await self.c.acknowledge_tool("look")
        self.client.playback_pending = False
        for name in ("ignore", "add_observation", "get_checklist", "ask_expert", "get_investigations"):
            await self.c.acknowledge_tool(name)
        self.assertFalse(self.events)

    async def test_interrupt_before_pcm_drops_feedback_and_releases_pending_playback(self):
        async def interrupt(event):
            self.events.append(event)
            if event["type"] == "audio_start":
                self.c.input_revision += 1
                self.c.user_speaking = True
        self.phone.send_json.side_effect = interrupt
        await self.c.acknowledge_tool("look")
        self.phone.send_bytes.assert_not_awaited()
        self.assertFalse(self.client.playback_pending)
        self.assertIn({"type": "flush"}, self.events)

    async def test_cancel_while_client_send_waits_cannot_leak_old_pcm(self):
        audible = True
        await self.client.lock.acquire()
        work = asyncio.create_task(self.client.send_audio(b"old", guard=lambda: audible))
        await asyncio.sleep(0)
        audible = False
        self.client.lock.release()
        await work
        self.phone.send_bytes.assert_not_awaited()

    async def test_expert_keeps_only_existing_scheduled_confirmation(self):
        self.c.tool = AsyncMock(return_value={"ok": True, "answer": "Investigation R1 is scheduled."})
        self.c.speak_expert = AsyncMock()
        await self.c.finish_route({"status": "completed", "output": [{"type": "function_call",
            "name": "ask_expert", "call_id": "expert", "arguments": '{"question":"What does it eat?"}'}]})
        self.phone.send_bytes.assert_not_awaited()
        self.c.speak_expert.assert_awaited_once_with("Investigation R1 is scheduled.")

    async def test_voice_receipt_is_immediate_even_when_another_tool_owns_worker(self):
        self.c.active = True
        self.c.phase = "route"
        self.c.generate = AsyncMock()
        await self.c.handle_rt({"type": "conversation.item.input_audio_transcription.completed",
            "transcript": "How many species have I seen today?", "item_id": "question-2"})
        self.assertIn("I heard you.", [e.get("text") for e in self.events])
        self.phone.send_bytes.assert_awaited_once()
        self.c.generate.assert_not_awaited()
        self.assertTrue(self.c.pending[-1][1].acknowledged)
        self.assertEqual(self.c.pending[-1][1].item_ids, {"question-2"})

    async def test_typed_receipt_and_already_acknowledged_tool_do_not_duplicate_preamble(self):
        await self.c.handle_phone({"type": "text", "text": "这里有哪些鸟？"}, self.client)
        self.assertTrue(any(e.get("text") == "听到了，我来处理。" for e in self.events))
        self.assertTrue(self.c.transcript_is_echo("听到了，我来处理。"))
        self.c.input_acknowledged = self.c.pending[-1][1].acknowledged
        self.c.tool = AsyncMock(return_value={"ok": True})
        self.c.generate = AsyncMock()
        before = self.phone.send_bytes.await_count
        await self.c.finish_route({"status": "completed", "output": [{"type": "function_call",
            "name": "nearby_species", "call_id": "check", "arguments": "{}"}]})
        self.assertEqual(self.phone.send_bytes.await_count, before)
        self.c.tool.assert_awaited_once()

    async def test_receipt_preserves_quiet_stop_filler_and_echo_policies(self):
        self.c.quiet = True
        await self.c.handle_rt({"type": "conversation.item.input_audio_transcription.completed",
            "transcript": "What does it eat?", "item_id": "quiet-1"})
        self.phone.send_bytes.assert_not_awaited()
        self.c.quiet = False
        self.c.pending.clear()
        await self.c.handle_rt({"type": "conversation.item.input_audio_transcription.completed",
            "transcript": "stop", "item_id": "stop-1"})
        self.phone.send_bytes.assert_not_awaited()
        self.assertFalse(self.c.pending)
        self.assertFalse(await self.c.acknowledge_input("uh"))
        self.c.remember_spoken(("received", "en"), "I heard you.")
        await self.c.handle_rt({"type": "conversation.item.input_audio_transcription.completed",
            "transcript": "I heard you.", "item_id": "echo-1"})
        self.phone.send_bytes.assert_not_awaited()
        self.assertFalse(self.c.pending)

    def test_all_bundled_clips_are_short_valid_pcm(self):
        for key, text in CLIPS.items():
            actual, pcm = recording(*key)
            self.assertEqual(actual, text)
            self.assertEqual(len(pcm) % 2, 0)
            self.assertTrue(0 < len(pcm) <= 6 * 48000)


if __name__ == "__main__":
    unittest.main()
