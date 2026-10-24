#!/usr/bin/env python3
"""Hands-on probes for Boson Higgs Realtime. Key: $BOSON_API_KEY or ~/.config/boson/api_key.
Usage: probe.py <test> ...   tests: voices text latency_text tts_in tools websearch bargein all
Outputs (wav/json) go to ./out/."""
import asyncio, base64, json, os, sys, time, wave, pathlib
import requests, websockets, numpy as np

KEY = os.environ.get("BOSON_API_KEY") or pathlib.Path("~/.config/boson/api_key").expanduser().read_text().strip()
H = {"Authorization": f"Bearer {KEY}"}
API = "https://api.boson.ai/v1"
WS = "wss://api.boson.ai/v1/realtime?model=higgs-realtime"
OUT = pathlib.Path(__file__).parent / "out"; OUT.mkdir(exist_ok=True)
SR = 24000
RESULTS = {}

def log(*a): print(*a, flush=True)

def save_wav(name, pcm: bytes, sr=SR):
    p = OUT / f"{name}.wav"
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr); w.writeframes(pcm)
    return str(p)

def tts(text, voice="default"):
    r = requests.post(f"{API}/audio/speech", headers=H, json={
        "model": "higgs-tts-3", "input": text, "voice": voice, "stream": True, "response_format": "pcm"},
        stream=True, timeout=120)
    r.raise_for_status()
    return b"".join(r.iter_content(4096))

async def connect(session):
    ws = await websockets.connect(WS, additional_headers=H, max_size=None)
    await ws.send(json.dumps({"type": "session.update", "session": {"model": "higgs-realtime", **session}}))
    return ws

async def collect(ws, until=("response.done",), timeout=60, t0=None, n_done=1, on=None):
    """Collect events until n_done terminal events. Returns dict with text, audio, events, timings."""
    t0 = t0 or time.perf_counter()
    res = {"events": [], "audio": bytearray(), "text": "", "transcript": "", "first_audio": None,
           "first_text": None, "calls": []}
    done = 0
    try:
        while done < n_done:
            msg = await asyncio.wait_for(ws.recv(), timeout)
            e = json.loads(msg); t = time.perf_counter() - t0; ty = e["type"]
            res["events"].append((round(t, 3), ty))
            if ty == "response.output_audio.delta":
                if res["first_audio"] is None: res["first_audio"] = t
                res["audio"] += base64.b64decode(e["delta"])
            elif ty == "response.output_text.delta":
                if res["first_text"] is None: res["first_text"] = t
                res["text"] += e["delta"]
            elif ty == "response.output_audio_transcript.delta":
                if res["first_text"] is None: res["first_text"] = t
                res["transcript"] += e["delta"]
            elif ty == "error":
                log("ERROR", e)
            elif ty == "response.done":
                for it in e["response"].get("output", []):
                    if it.get("type") == "function_call": res["calls"].append(it)
                res["status"] = e["response"].get("status")
            if on: await on(e, t)
            if ty in until: done += 1
    except asyncio.TimeoutError:
        res["timeout"] = True
    return res

async def say_text(ws, text):
    await ws.send(json.dumps({"type": "conversation.item.create", "item": {
        "type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}}))
    await ws.send(json.dumps({"type": "response.create"}))

async def stream_pcm(ws, pcm, chunk_ms=40, realtime=True):
    step = SR * 2 * chunk_ms // 1000
    for i in range(0, len(pcm), step):
        await ws.send(json.dumps({"type": "input_audio_buffer.append",
                                  "audio": base64.b64encode(pcm[i:i+step]).decode()}))
        if realtime: await asyncio.sleep(chunk_ms / 1000)

def silence(ms): return b"\x00\x00" * (SR * ms // 1000)

# ---------------- tests ----------------
def t_voices():
    r = requests.get(f"{API}/audio/voices", headers=H, timeout=30)
    log("voices", r.status_code); d = r.json()
    items = d.get("data", d) if isinstance(d, dict) else d
    names = [v.get("voice") or v.get("id") or v.get("name") for v in items] if isinstance(items, list) else items
    log(json.dumps(names, ensure_ascii=False)[:2000]); RESULTS["voices"] = names

TEXT_PROMPTS = [
    ("math", "A train leaves at 3:40 pm and the trip takes 2 hours 35 minutes. When does it arrive? Then: what is 17*23? Answer briefly."),
    ("reason", "I have 3 boxes: one has only apples, one only oranges, one mixed. All labels are wrong. I may draw one fruit from one box. Which box do I draw from and why? Be brief."),
    ("instr", "Reply with exactly three bullet points, each under six words, listing uses of a paperclip. Nothing else."),
    ("zh", "用中文简单解释一下什么是 speculative decoding，两三句话就行。"),
    ("code", "Write a Python one-liner that returns the second largest distinct number in list xs."),
    ("persona", "Who are you, which company built you, and what model are you?"),
]

async def t_text():
    out = {}
    for name, p in TEXT_PROMPTS:
        ws = await connect({"output_modalities": ["text"], "instructions": "You are a helpful assistant."})
        t0 = time.perf_counter(); await say_text(ws, p)
        r = await collect(ws, t0=t0)
        out[name] = {"ttft": r["first_text"], "total": r["events"][-1][0] if r["events"] else None, "text": r["text"]}
        log(f"--- {name} ttft={r['first_text']} \n{r['text']}\n"); await ws.close()
    RESULTS["text"] = out

async def t_latency_text():
    """text in -> audio out latency, 5 trials, plus save audio."""
    out = []
    ws = await connect({"output_modalities": ["audio"], "instructions": "You are a friendly voice assistant. Keep answers to one or two sentences."})
    await collect(ws, until=("session.updated",), timeout=15)
    for i, p in enumerate(["Hi! How are you today?", "Tell me a fun fact about owls.",
                           "你好，今天多伦多天气适合出门吗？随便聊两句。", "What's a good name for a cat?",
                           "Say something with excitement, like you just won a prize!"]):
        t0 = time.perf_counter(); await say_text(ws, p)
        r = await collect(ws, t0=t0)
        dur = len(r["audio"]) / 2 / SR
        f = save_wav(f"latency_text_{i}", bytes(r["audio"]))
        out.append({"prompt": p, "first_audio_s": r["first_audio"], "audio_s": round(dur, 2),
                    "gen_done_s": r["events"][-1][0], "transcript": r["transcript"], "wav": f})
        log(out[-1])
    await ws.close(); RESULTS["latency_text"] = out

async def t_tts_in():
    """Speech in (TTS-generated question streamed at real-time pace, server_vad) -> measure end-of-speech -> first audio."""
    out = []
    qs = ["Hey, can you tell me what the capital of Australia is?",
          "I'm going to a hackathon tonight. Give me one tip to stay focused.",
          "你能用一句话说说为什么天空是蓝色的吗？"]
    for vad in ["server_vad", "semantic_vad"]:
        ws = await connect({"output_modalities": ["audio"], "instructions": "Keep answers short.",
                            "audio": {"input": {"turn_detection": {"type": vad},
                                                "transcription": {"model": "higgs-stt-3.1"}}}})
        await collect(ws, until=("session.updated",), timeout=15)
        for i, q in enumerate(qs):
            pcm = tts(q)
            t_start = time.perf_counter()
            sender = asyncio.create_task(stream_pcm(ws, pcm + silence(3000)))
            speech_end = len(pcm) / 2 / SR  # seconds after t_start when user speech ends (approx, includes TTS lead silence)
            marks = {}
            async def on(e, t):
                if e["type"] in ("input_audio_buffer.speech_started", "input_audio_buffer.speech_stopped",
                                 "input_audio_buffer.committed", "response.created") and e["type"] not in marks:
                    marks[e["type"]] = round(t, 3)
                if e["type"] == "conversation.item.input_audio_transcription.completed":
                    marks["user_transcript"] = e.get("transcript")
            r = await collect(ws, t0=t_start, on=on, timeout=40)
            sender.cancel()
            # trailing-silence-trim estimate of real speech end
            a = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
            nz = np.where(np.abs(a) > 500)[0]; real_end = (nz[-1] / SR) if len(nz) else speech_end
            rec = {"vad": vad, "q": q, "speech_end_s": round(real_end, 3), "marks": marks,
                   "first_audio_s": r["first_audio"],
                   "eos_to_first_audio_ms": round((r["first_audio"] - real_end) * 1000) if r["first_audio"] else None,
                   "transcript": r["transcript"]}
            save_wav(f"tts_in_{vad}_{i}_q", pcm); save_wav(f"tts_in_{vad}_{i}_a", bytes(r["audio"]))
            log(rec); out.append(rec)
            await asyncio.sleep(1.0)
        await ws.close()
    RESULTS["tts_in"] = out

TOOLS = [
    {"type": "function", "name": "get_session_status", "description": "Get the status of one of the user's coding agent sessions by name.",
     "parameters": {"type": "object", "properties": {"session": {"type": "string"}}, "required": ["session"]}},
    {"type": "function", "name": "send_message", "description": "Send a text instruction to a coding agent session.",
     "parameters": {"type": "object", "properties": {"session": {"type": "string"}, "text": {"type": "string"}}, "required": ["session", "text"]}},
    {"type": "function", "name": "set_timer", "description": "Set a timer.",
     "parameters": {"type": "object", "properties": {"minutes": {"type": "number"}, "label": {"type": "string"}}, "required": ["minutes"]}},
]
FAKE = {"get_session_status": lambda a: {"session": a.get("session"), "state": "tests_failed", "failing": ["test_parse_dates"], "last_commit": "fix tz"},
        "send_message": lambda a: {"ok": True}, "set_timer": lambda a: {"ok": True, "id": "t1"}}

async def t_tools():
    out = []
    cases = ["How is my session called backend doing?",
             "Check the frontend session, and if anything failed, tell it to fix the failing test.",
             "Set a 25 minute focus timer and also a 5 minute break timer after it.",
             "What's 2 plus 2?"]
    for c in cases:
        ws = await connect({"output_modalities": ["text"], "tools": TOOLS, "tool_choice": "auto",
                            "instructions": "You are a voice assistant that manages the user's coding agent sessions. Use tools when relevant."})
        await collect(ws, until=("session.updated",), timeout=15)
        t0 = time.perf_counter(); await say_text(ws, c)
        trace = []
        for step in range(5):
            r = await collect(ws, t0=t0, timeout=60)
            calls = [x for x in r["calls"] if x.get("executor", "client") == "client"]
            trace.append({"t": r["events"][-1][0] if r["events"] else None, "text": r["text"],
                          "calls": [(x["name"], x["arguments"]) for x in r["calls"]]})
            if not calls: break
            for x in calls:
                res = FAKE[x["name"]](json.loads(x["arguments"] or "{}"))
                await ws.send(json.dumps({"type": "conversation.item.create", "item": {
                    "type": "function_call_output", "call_id": x["call_id"], "output": json.dumps(res)}}))
            await ws.send(json.dumps({"type": "response.create"}))
        log("=== ", c); [log("   ", s) for s in trace]
        out.append({"case": c, "trace": trace}); await ws.close()
    RESULTS["tools"] = out

async def t_websearch():
    out = []
    for q in ["What is the current price of Bitcoin in US dollars?", "Who won the most recent Formula 1 race?"]:
        ws = await connect({"output_modalities": ["text"], "tools": [{"type": "web_search"}],
                            "instructions": "Search the web for anything current."})
        await collect(ws, until=("session.updated",), timeout=15)
        t0 = time.perf_counter(); await say_text(ws, q)
        r = await collect(ws, t0=t0, n_done=2, timeout=60)
        out.append({"q": q, "text": r["text"], "calls": [(x["name"], x.get("executor"), x["arguments"]) for x in r["calls"]],
                    "events": [e for e in r["events"] if not e[1].endswith("delta")]})
        log(out[-1]); await ws.close()
    RESULTS["websearch"] = out

async def t_bargein():
    """Ask for a long story, then interrupt with speech after 2 s of assistant audio. Measure speech start -> audio stop."""
    ws = await connect({"output_modalities": ["audio"], "instructions": "You are a storyteller.",
                        "audio": {"input": {"turn_detection": {"type": "server_vad"}}}})
    await collect(ws, until=("session.updated",), timeout=15)
    interrupt = tts("Wait, stop. Actually, just tell me the ending in one sentence.")
    t0 = time.perf_counter(); await say_text(ws, "Tell me a long story about a cat who lives inside a computer. At least two minutes.")
    st = {"first_audio": None, "sent": None, "speech_started": None, "last_audio_after_interrupt": None, "cancelled": None}
    # keep mic stream of silence running like a real client
    async def mic():
        while True:
            await ws.send(json.dumps({"type": "input_audio_buffer.append", "audio": base64.b64encode(silence(40)).decode()}))
            await asyncio.sleep(0.04)
    mic_task = asyncio.create_task(mic()); inter_task = None
    audio_before = bytearray(); audio_after = bytearray()
    try:
        while True:
            e = json.loads(await asyncio.wait_for(ws.recv(), 60)); t = time.perf_counter() - t0; ty = e["type"]
            if ty == "response.output_audio.delta":
                if st["first_audio"] is None: st["first_audio"] = t
                (audio_after if st["sent"] else audio_before).extend(base64.b64decode(e["delta"]))
                if st["sent"]: st["last_audio_after_interrupt"] = t
                if st["sent"] is None and t - st["first_audio"] > 2.0:
                    mic_task.cancel(); st["sent"] = t
                    inter_task = asyncio.create_task(stream_pcm(ws, interrupt + silence(2500)))
            elif ty == "input_audio_buffer.speech_started" and st["speech_started"] is None and st["sent"]:
                st["speech_started"] = t
            elif ty == "response.done":
                log("response.done", e["response"].get("status"), round(t, 3))
                if e["response"].get("status") == "cancelled": st["cancelled"] = t
                elif st["cancelled"] or (st["sent"] and t > st["sent"] + 3):
                    st["final_done"] = t; break
            elif ty == "response.output_audio_transcript.delta" and st.get("cancelled"):
                st.setdefault("reply", ""); st["reply"] += e["delta"]
    except asyncio.TimeoutError:
        st["timeout"] = True
    save_wav("bargein_before", bytes(audio_before)); save_wav("bargein_after", bytes(audio_after))
    st["audio_generated_before_interrupt_s"] = round(len(audio_before) / 2 / SR, 2)
    log(st); RESULTS["bargein"] = st; await ws.close()

TESTS = {"voices": t_voices, "text": t_text, "latency_text": t_latency_text, "tts_in": t_tts_in,
         "tools": t_tools, "websearch": t_websearch, "bargein": t_bargein}

async def main(names):
    if names == ["all"]: names = list(TESTS)
    for n in names:
        log(f"\n######## {n}")
        try:
            f = TESTS[n]; r = f(); 
            if asyncio.iscoroutine(r): await r
        except Exception as ex:
            log("TEST FAILED", n, repr(ex)); RESULTS[n + "_error"] = repr(ex)
    p = OUT / f"results_{int(time.time())}.json"; p.write_text(json.dumps(RESULTS, ensure_ascii=False, indent=1, default=str))
    log("saved", p)

if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:] or ["all"]))
