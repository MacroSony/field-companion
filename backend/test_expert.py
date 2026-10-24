"""Offline expert contract, SDK transport and session integration regressions."""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx2
from openai import AsyncOpenAI

import expert as e
import server as s


def observation():
    return {"id": "ob1", "status": "open", "uncertain": True, "hint": "", "group": "Aves",
            "lat": 43.66, "lng": -79.39, "t_start": "2026-10-23T15:00:00-04:00",
            "fused": [{"sci": "Dryobates pubescens", "en": "Downy Woodpecker", "p": .55},
                      {"sci": "Leuconotopicus villosus", "en": "Hairy Woodpecker", "p": .45}],
            "detections": [{"id": "d1", "kind": "image", "t": "now",
                            "thumb": "/api/frame/d1.jpg", "candidates": [
                                {"sci": "Dryobates pubescens", "en": "Downy Woodpecker", "p": .55}]}]}


def decision(**changes):
    data = {"action": "request_more_evidence", "basis": "insufficient",
            "answer": "Could you get a closer view of the head?", "hint": "Zoom on the head",
            "evidence_ids": ["d1"], "source_ids": [], "source_quotes": []}
    data.update(changes)
    return data


class ExpertProviderTests(unittest.IsolatedAsyncioTestCase):
    def provider(self, output=None, *, settings=None, knowledge=None, delay=0, error=None, http_status=200):
        self.bodies = []
        async def handler(request):
            self.assertEqual(str(request.url), "https://api.openai.com/v1/responses")
            self.bodies.append(json.loads(request.content))
            if delay:
                await asyncio.sleep(delay)
            if error:
                return httpx2.Response(http_status, json={"error": error}, headers={"retry-after": "15"})
            return httpx2.Response(200, json={
                "id": "resp_test", "object": "response", "created_at": 1,
                "status": "completed", "model": "gpt-5.4-mini", "output": [{
                    "type": "message", "id": "msg_test", "role": "assistant", "status": "completed",
                    "content": [{"type": "output_text", "text": json.dumps(output or decision()),
                                 "annotations": []}]}]})
        def factory(**kwargs):
            return AsyncOpenAI(**kwargs, http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))
        return e.NaturalistExpert(settings or e.Settings(api_key="test-placeholder"),
                                 knowledge or SimpleNamespace(sources=AsyncMock(return_value=[])), factory)

    async def test_real_sdk_structured_request_and_followup(self):
        provider = self.provider()
        obs = observation()
        original = deepcopy(obs)
        result = await provider.answer("Could it be a Hairy Woodpecker?", obs)
        self.assertTrue(result["ok"])
        self.assertEqual(result["revision"], e.revision(obs))
        self.assertEqual(obs, original)
        body = self.bodies[0]
        self.assertFalse(body["store"])
        self.assertNotIn("tools", body)
        self.assertTrue(body["text"]["format"]["strict"])
        self.assertFalse(body["text"]["format"]["schema"]["additionalProperties"])
        self.assertNotIn("/api/frame", body["input"])
        self.assertNotIn('"p"', body["input"])
        self.assertIn('"score"', body["input"])
        self.assertIn("Required response language: English.", body["instructions"])
        self.assertEqual(body["text"]["format"]["schema"]["$defs"]["EvidenceId"]["enum"], ["d1"])

    async def test_empty_context_cannot_generate_references(self):
        obs = observation()
        obs["detections"], obs["fused"] = [], []
        provider = self.provider(decision(evidence_ids=[]))
        self.assertTrue((await provider.answer("What is it?", obs))["ok"])
        properties = self.bodies[0]["text"]["format"]["schema"]["properties"]
        for name in ("evidence_ids", "source_ids", "source_quotes"):
            self.assertEqual(properties[name]["maxItems"], 0)

    async def test_answer_language_matches_question(self):
        for question, answer, expected in (("What is it?", "Please get a closer view.", True),
                ("What is it?", "请拍近一点。", False),
                ("这是什么？", "请拍近一点。", True),
                ("这是什么？", "Please get a closer view.", False)):
            with self.subTest(question=question, answer=answer):
                result = await self.provider(decision(answer=answer)).answer(question, observation())
                self.assertEqual(result["ok"], expected)

    async def test_species_fact_retains_only_cited_source_metadata(self):
        source = {"id": "wiki:Dryobates pubescens", "sci": "Dryobates pubescens",
                  "title": "Downy woodpecker", "url": "https://en.wikipedia.org/wiki/Downy_woodpecker",
                  "extract": "TEST SOURCE TEXT"}
        provider = self.provider(decision(action="answer", basis="species_knowledge",
            answer="If it is a Downy Woodpecker, the supplied source describes its diet.",
            hint="", source_ids=[source["id"]], source_quotes=[{"source_id": source["id"], "quote": "TEST SOURCE TEXT"}]),
            knowledge=SimpleNamespace(sources=AsyncMock(return_value=[source])))
        result = await provider.answer("What does it eat?", observation())
        self.assertTrue(result["ok"])
        self.assertEqual(result["sources"][0]["url"], source["url"])
        self.assertNotIn("extract", result["sources"][0])

    async def test_unbacked_sources_evidence_and_empty_hint_rejected(self):
        for changes in ({"source_ids": ["invented"]}, {"evidence_ids": ["other-observation"]},
                        {"hint": " "}, {"basis": "species_knowledge"}):
            with self.subTest(changes=changes):
                result = await self.provider(decision(**changes)).answer("What is it?", observation())
                self.assertEqual(result["status"], "invalid_response")
                self.assertEqual(result["action"], "abstain")

    async def test_chinese_facts_require_supplied_species_identifier(self):
        source = {"id": "wiki:Dryobates pubescens", "sci": "Dryobates pubescens",
                  "title": "Downy woodpecker", "url": "https://en.wikipedia.org/wiki/Downy_woodpecker",
                  "extract": "TEST SOURCE TEXT"}
        for answer, expected in (("如果是未经核实的中文鸟名，它吃昆虫。", False),
                                 ("如果是 Downy Woodpecker，它吃昆虫。", True),
                                 ("如果是 Dryobates pubescens，它吃昆虫。", True)):
            with self.subTest(answer=answer):
                provider = self.provider(decision(action="answer", basis="species_knowledge", answer=answer,
                    hint="", source_ids=[source["id"]], source_quotes=[{"source_id": source["id"], "quote": "TEST SOURCE TEXT"}]),
                    knowledge=SimpleNamespace(sources=AsyncMock(return_value=[source])))
                result = await provider.answer("它吃什么？", observation())
                self.assertEqual(result["ok"], expected)

    async def test_fact_quotes_must_match_each_cited_source(self):
        source = {"id": "wiki:Dryobates pubescens", "sci": "Dryobates pubescens",
                  "title": "Downy woodpecker", "url": "https://en.wikipedia.org/wiki/Downy_woodpecker",
                  "extract": "It feeds primarily on insects. It also eats seeds."}
        for quotes, expected in (([], False),
                ([{"source_id": source["id"], "quote": "It feeds primarily on insects."}], True),
                ([{"source_id": source["id"], "quote": "It eats fish."}], False),
                ([{"source_id": "wiki:another", "quote": "It feeds primarily on insects."}], False)):
            with self.subTest(quotes=quotes):
                provider = self.provider(decision(action="answer", basis="species_knowledge",
                    answer="Downy Woodpecker feeds primarily on insects.", hint="",
                    source_ids=[source["id"]], source_quotes=quotes),
                    knowledge=SimpleNamespace(sources=AsyncMock(return_value=[source])))
                result = await provider.answer("What does it eat?", observation())
                self.assertEqual(result["ok"], expected)

    async def test_missing_key_and_disabled_make_no_requests(self):
        for settings in (e.Settings(), e.Settings(api_key="test", enabled=False)):
            provider = self.provider(settings=settings)
            result = await provider.answer("它吃什么？", observation())
            self.assertEqual(result["status"], "not_configured")
            self.assertIn("专家", result["answer"])
            self.assertEqual(self.bodies, [])

    async def test_deadline_cancels_slow_request_without_retry(self):
        result = await self.provider(settings=e.Settings(api_key="test", timeout=.5), delay=2).answer(
            "What is it?", observation())
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(len(self.bodies), 1)

    async def test_service_error_does_not_log_exception_contents(self):
        provider = self.provider()
        provider.client_factory = Mock(side_effect=RuntimeError("SECRET-MARKER"))
        with self.assertLogs(e.LOG, level="WARNING") as logs:
            result = await provider.answer("What is it?", observation())
        self.assertEqual(result["status"], "unavailable")
        self.assertNotIn("SECRET-MARKER", " ".join(logs.output))

    async def test_api_failures_and_rate_limit_backoff(self):
        for status_code, code, expected in ((429, "insufficient_quota", "quota_exhausted"),
                (429, "rate_limit_exceeded", "rate_limited"), (401, "invalid_api_key", "authentication_failed"),
                (403, "permission_denied", "access_denied"), (404, "model_not_found", "access_denied"),
                (500, "server_error", "unavailable")):
            with self.subTest(code=code):
                provider = self.provider(http_status=status_code, error={
                    "code": code, "message": "Test error", "type": "test"})
                with self.assertLogs(e.LOG, level="WARNING"):
                    result = await provider.answer("What is it?", observation())
                self.assertEqual(result["status"], expected)
                self.assertEqual(len(self.bodies), 1)
                if status_code == 429:
                    await provider.answer("What is it?", observation())
                    self.assertEqual(len(self.bodies), 1)

    async def test_cancellation_propagates(self):
        provider = self.provider()
        provider.knowledge.sources.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await provider.answer("What is it?", observation())

    async def test_bad_question_and_bounded_context(self):
        provider = self.provider()
        for question in ("", " ", "a" * 2001, None):
            with self.assertRaises(ValueError):
                await provider.answer(question, observation())
        obs = observation()
        obs["detections"] *= 100
        obs["detections"][0]["text"] = "x" * 10000
        compact = e.compact_observation(obs)
        self.assertEqual(len(compact["detections"]), 12)
        self.assertEqual(len(compact["detections"][0]["text"]), 600)

    async def test_knowledge_cache_and_exact_species_title(self):
        knowledge = e.SpeciesKnowledge()
        with patch.object(knowledge, "fetch", return_value={"id": "wiki:test"}) as fetch:
            self.assertIsNone(await knowledge.get("unknown bird"))
            await knowledge.get("Dryobates pubescens")
            await knowledge.get("Dryobates pubescens")
            fetch.assert_called_once_with("Dryobates pubescens")

    async def test_knowledge_missing_disambiguation_and_failure(self):
        knowledge = e.SpeciesKnowledge()
        for page in ({"missing": True}, {"title": "X", "extract": "text", "pageprops": {"disambiguation": ""}}):
            response = Mock(json=lambda: {"query": {"pages": [page]}})
            with patch.object(e.requests, "get", return_value=response):
                self.assertIsNone(knowledge.fetch("Dryobates pubescens"))
        with patch.object(knowledge, "fetch", side_effect=e.requests.Timeout):
            self.assertIsNone(await knowledge.get("Dryobates pubescens"))

    async def test_unreadable_local_config_keeps_core_available(self):
        with patch.object(e, "dotenv_values", side_effect=PermissionError), patch.dict(e.os.environ, {}, clear=True):
            self.assertFalse(e.Settings.load().api_key)


class ExpertSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.c = s.Session("expert-test")
        self.c.allowed_species = lambda: set()
        self.c.rt_send = AsyncMock()
        self.c.generate = AsyncMock()
        self.c.phone_json = AsyncMock()
        self.c.expert = SimpleNamespace(available=True, answer=AsyncMock(return_value={
            "ok": True, "status": "completed", **decision()}))
        self.obs = s.Observation("ob1", detections=[s.Detection("d1", "image", s.now_iso(),
            [{"sci": "Dryobates pubescens", "en": "Downy Woodpecker", "p": .55}])],
            fused=observation()["fused"])
        self.c.observations[self.obs.id] = self.obs
        self.c.focus = self.obs.id

    async def asyncTearDown(self):
        await self.c.close()

    async def route(self, name, args=None):
        await self.c.finish_route({"status": "completed", "output": [{"type": "function_call",
            "name": name, "call_id": "c1", "arguments": json.dumps(args or {})}]})

    async def test_expert_hint_reused_by_look_again_without_logging(self):
        self.c.mic = SimpleNamespace(send_json=AsyncMock())
        fused = deepcopy(self.obs.fused)
        await self.route("ask_expert", {"question": "Could it be a Hairy?"})
        self.c.expert.answer.assert_awaited_once()
        self.assertEqual(self.obs.hint, "Zoom on the head")
        self.assertEqual(self.obs.fused, fused)
        self.assertEqual(self.c.items, [])
        self.assertEqual(self.obs.status, "open")
        await self.c.tool("look", {})
        self.assertEqual(self.c.focus, "ob1")
        self.assertEqual(self.c.mic.send_json.call_args.args[0]["hint"], "Zoom on the head")

    async def test_focus_changes_evidence_changes_and_new_questions_drop_results(self):
        async def change_focus(*args):
            await self.c.new_focus()
            return {"ok": True, **decision()}
        self.c.expert.answer.side_effect = change_focus
        await self.route("ask_expert", {"question": "What is it?"})
        self.c.generate.assert_not_awaited()
        self.assertEqual(self.obs.hint, "")

        for mutation in (lambda: self.obs.detections.append(s.Detection("d2", "image", s.now_iso())),
                         lambda: setattr(self.c, "input_revision", self.c.input_revision + 1)):
            self.c.focus = self.obs.id
            self.obs.status = "open"
            async def mutate(*args):
                mutation()
                return {"ok": True, **decision()}
            self.c.expert.answer.side_effect = mutate
            result = await self.c.tool("ask_expert", {"question": "What is it?"})
            self.assertTrue(result["suppress_answer"])
            self.assertEqual(self.obs.hint, "")

    async def test_focus_change_during_hint_broadcast_suppresses_speech(self):
        async def change_focus(event):
            self.c.focus = "other"
        self.c.phone_json.side_effect = change_focus
        await self.route("ask_expert", {"question": "What is it?"})
        self.c.generate.assert_not_awaited()

    async def test_new_input_during_tool_output_suppresses_speech(self):
        async def new_input(event):
            self.c.input_revision += 1
        self.c.rt_send.side_effect = new_input
        await self.route("ask_expert", {"question": "What is it?"})
        self.c.generate.assert_not_awaited()

    async def test_speaker_echo_keeps_expert_context_but_real_barge_in_invalidates(self):
        client = s.Client(SimpleNamespace(send_json=AsyncMock()), self.c)
        self.c.clients.add(client)
        self.c.mic = client
        for earphones in (False, True):
            client.earphones = earphones
            self.c.play_until = s.time.monotonic() + 2
            self.c.user_speaking = self.c.awaiting_transcript = False
            async def incoming_audio(*args):
                await self.c.handle_rt({"type": "input_audio_buffer.speech_started"})
                return {"ok": True, "status": "completed", **decision()}
            self.c.expert.answer.side_effect = incoming_audio
            result = await self.c.tool("ask_expert", {"question": "What is it?"})
            self.assertEqual(bool(result.get("suppress_answer")), earphones)
            self.assertEqual(self.c.user_speaking, earphones)

    async def test_simple_checklist_and_count_paths_never_call_expert(self):
        self.obs.status, self.obs.count = "confirmed", 2
        await self.route("get_checklist")
        await self.route("set_count", {"name": "Downy Woodpecker", "count": 4})
        self.assertEqual(self.c.items[0]["count"], 4)
        self.c.expert.answer.assert_not_awaited()

    async def test_missing_focus_unavailable_and_quiet(self):
        self.c.focus = None
        result = await self.c.tool("ask_expert", {"question": "What does it eat?"})
        self.assertFalse(result["ok"])
        self.c.expert.answer.assert_not_awaited()
        self.c.focus = self.obs.id
        self.c.quiet = True
        self.c.expert.answer.return_value = e.fallback("What does it eat?", "timeout")
        await self.route("ask_expert", {"question": "What does it eat?"})
        self.c.generate.assert_not_awaited()
        self.assertEqual(self.c.items, [])

    async def test_visual_expert_only_for_uncertain_observation(self):
        await self.c.answer_vision(self.obs.id)
        self.c.expert.answer.assert_awaited_once()
        self.assertIn("[EXPERT]", self.c.rt_send.call_args.args[0]["item"]["content"][0]["text"])
        self.c.expert.answer.reset_mock()
        self.obs.uncertain = False
        await self.c.answer_vision(self.obs.id)
        self.c.expert.answer.assert_not_awaited()
        self.assertIn("[VISION]", self.c.rt_send.call_args.args[0]["item"]["content"][0]["text"])

    async def test_visual_expert_failure_uses_original_vision_path(self):
        self.c.expert.answer.return_value = e.fallback("What is it?", "timeout")
        await self.c.answer_vision(self.obs.id)
        self.assertIn("[VISION]", self.c.rt_send.call_args.args[0]["item"]["content"][0]["text"])
        self.c.generate.assert_awaited_once_with("answer")

    async def test_late_frame_uses_latest_vision_without_old_expert_answer(self):
        async def late_frame(*args):
            self.obs.detections.append(s.Detection("d2", "image", s.now_iso()))
            return {"ok": True, **decision(answer="OLD EXPERT ANSWER")}
        self.c.expert.answer.side_effect = late_frame
        await self.c.answer_vision(self.obs.id)
        content = self.c.rt_send.call_args.args[0]["item"]["content"][0]["text"]
        self.assertIn("[VISION]", content)
        self.assertNotIn("OLD EXPERT ANSWER", content)

    async def test_new_question_during_vision_suppresses_all_old_speech(self):
        async def new_question(*args):
            self.c.input_revision += 1
            return {"ok": True, **decision()}
        self.c.expert.answer.side_effect = new_question
        await self.c.answer_vision(self.obs.id)
        self.c.generate.assert_not_awaited()


if __name__ == "__main__":
    unittest.main(verbosity=2)
