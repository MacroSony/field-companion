#!/usr/bin/env python3
"""Voice checklist + feature ID probe: Higgs Realtime + iNat (nearby list, name resolution). mode: text|audio"""
import sys, json, time, asyncio, urllib.parse, requests
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
from realtime_probe import connect, collect, say_text, tts, stream_pcm, silence, OUT

LAT, LNG = 43.66, -79.39   # Toronto downtown (demo location)
CHECKLIST = []
_nearby_cache = {}

def inat(path, **p):
    r = requests.get("https://api.inaturalist.org/v1/" + path, params=p, timeout=20, headers={"User-Agent": "mika-hackathon-probe"})
    r.raise_for_status(); return r.json()

def nearby_species(group="Aves", limit=60):
    if group not in _nearby_cache:
        d = inat("observations/species_counts", lat=LAT, lng=LNG, radius=15, d1="2026-09-01", iconic_taxa=group, per_page=limit, locale="zh-CN")
        from fieldmarks import FM
        _nearby_cache[group] = [{"sci": r["taxon"]["name"], "en": r["taxon"].get("english_common_name"), "zh": r["taxon"].get("preferred_common_name"), "n": r["count"], "marks": FM.get(r["taxon"].get("english_common_name") or "", "")} for r in d["results"]]
    return _nearby_cache[group]

def resolve(name, group=None):
    d = inat("taxa/autocomplete", q=name, locale="zh-CN", per_page=10)
    res = [t for t in d["results"] if (group is None or t.get("iconic_taxon_name") == group)]
    sp = [t for t in res if t.get("rank") == "species"] or res
    if not sp: return None
    t = sp[0]; return {"sci": t["name"], "en": t.get("english_common_name"), "zh": t.get("preferred_common_name"), "rank": t.get("rank")}

def tool(name, a):
    if name == "nearby_species":
        return {"location": "Toronto", "since": "2026-09-01", "species": nearby_species(a.get("group", "Aves"))}
    if name == "add_observation":
        t = resolve(a["name"], a.get("group"))
        if not t: return {"ok": False, "error": f"no taxon matches '{a['name']}'"}
        for row in CHECKLIST:
            if row["sci"] == t["sci"]:
                row["count"] += int(a.get("count", 1)); row["notes"] += ("; " + a["notes"]) if a.get("notes") else ""; return {"ok": True, "updated": row}
        row = {**t, "count": int(a.get("count", 1)), "notes": a.get("notes", "")}; CHECKLIST.append(row); return {"ok": True, "added": row}
    if name == "remove_observation":
        t = resolve(a["name"], a.get("group")); before = len(CHECKLIST)
        CHECKLIST[:] = [r for r in CHECKLIST if not t or r["sci"] != t["sci"]]; return {"ok": True, "removed": before - len(CHECKLIST)}
    if name == "get_checklist": return {"checklist": CHECKLIST}
    return {"error": "unknown tool"}

GROUPS = ["Aves", "Insecta", "Plantae", "Mammalia", "Fungi", "Reptilia", "Amphibia"]
TOOLS = [
 {"type": "function", "name": "nearby_species", "description": "List species recently observed near the user (iNaturalist), with Chinese and English common names. Use it to narrow down an identification from a description.",
  "parameters": {"type": "object", "properties": {"group": {"type": "string", "enum": GROUPS}}, "required": ["group"]}},
 {"type": "function", "name": "add_observation", "description": "Add an organism to today's checklist (or increase its count). name may be a Chinese/English common name or scientific name.",
  "parameters": {"type": "object", "properties": {"name": {"type": "string"}, "group": {"type": "string", "enum": GROUPS}, "count": {"type": "integer"}, "notes": {"type": "string"}}, "required": ["name", "group"]}},
 {"type": "function", "name": "remove_observation", "description": "Remove a species from the checklist.", "parameters": {"type": "object", "properties": {"name": {"type": "string"}, "group": {"type": "string", "enum": GROUPS}}, "required": ["name"]}},
 {"type": "function", "name": "get_checklist", "description": "Read today's checklist.", "parameters": {"type": "object", "properties": {}}},
]
INSTR = ("You are a hands-free field companion for a birder/naturalist in Toronto. The user is holding binoculars or a camera, so keep replies to one short sentence. "
         "When the user reports an organism, add it with add_observation. When the user describes something they can't name, call nearby_species for that group and suggest the 1-2 most likely species, "
         "then ask before adding. Never invent species that aren't plausible here. Reply in the user's language.")

CASES_ID = [
  ("有一只灰色背、橙色胸脯的鸟在草坪上走来走去，是什么？", "Turdus migratorius"),
  ("I see a small black and white bird pecking on a tree trunk, with a little red patch on the back of the head. What is it?", "Dryobates pubescens"),
  ("一群棕灰色带冠的鸟在吃浆果，脸上有黑色眼罩，尾巴尖是黄色的。", "Bombycilla cedrorum"),
  ("A dark gray bird with a white belly and pink bill hopping on the ground, white flashes on the tail when it flies.", "Junco hyemalis"),
  ("一只全身亮蓝色、头上有冠的鸟，叫声很吵。", "Cyanocitta cristata"),
  ("A small bird climbing down the tree head-first, orange belly, black stripe through the eye.", "Sitta canadensis"),
  ("水边一只蓝灰色、头很大、乱蓬蓬的冠，嘴很长，飞的时候发出嘎嘎的连串叫声。", "Megaceryle alcyon"),
  ("Black bird with red and yellow shoulder patches in the cattails.", "Agelaius phoeniceus"),
]
CASES = [
  ("两只北美红雀，一公一母，在灌木丛里。", lambda c: any(r["sci"] == "Cardinalis cardinalis" and r["count"] == 2 for r in c)),
  ("Add three mallards on the pond.", lambda c: any(r["sci"] == "Anas platyrhynchos" and r["count"] == 3 for r in c)),
  ("有一只灰色背、橙色胸脯的鸟在草坪上走来走去，是什么？", None),
  ("对，记上。", lambda c: any(r["sci"] == "Turdus migratorius" for r in c)),
  ("I see a small black and white bird pecking on a tree trunk, with a little red patch on the back of the head. What is it?", None),
  ("Yes, add one.", lambda c: any(r["sci"] in ("Dryobates pubescens", "Leuconotopicus villosus") for r in c)),
  ("还有一只君主斑蝶在乳草上。", lambda c: any(r["sci"] == "Danaus plexippus" for r in c)),
  ("Oh wait, the mallards were actually four, not three.", lambda c: any(r["sci"] == "Anas platyrhynchos" and r["count"] == 4 for r in c)),
  ("我们现在记了多少种？", None),
]

async def run(mode):
    CHECKLIST.clear()
    sess = {"output_modalities": ["text" if mode == "text" else "audio"], "instructions": INSTR, "tools": TOOLS}
    if mode == "audio": sess["audio"] = {"input": {"turn_detection": {"type": "server_vad"}, "transcription": {"model": "higgs-stt-3.1"}}}
    ws = await connect(sess); await collect(ws, until=("session.updated",), timeout=15)
    for utt, check in CASES:
        t0 = time.perf_counter(); sender = None
        if mode == "text": await say_text(ws, utt)
        else: sender = asyncio.create_task(stream_pcm(ws, tts(utt) + silence(1500)))
        said = ""; calls = []; heard = None
        for _ in range(6):
            mk = {}
            async def on(e, t):
                if e["type"] == "conversation.item.input_audio_transcription.completed": mk["h"] = e.get("transcript")
            r = await collect(ws, t0=t0, timeout=45, on=on); heard = heard or mk.get("h")
            said += (r["text"] or r["transcript"]) + " "
            cc = [x for x in r["calls"] if x.get("executor", "client") == "client"]
            if not cc: break
            for x in cc:
                a = json.loads(x["arguments"] or "{}"); res = tool(x["name"], a); calls.append((x["name"], a))
                await ws.send(json.dumps({"type": "conversation.item.create", "item": {"type": "function_call_output", "call_id": x["call_id"], "output": json.dumps(res, ensure_ascii=False)}}))
            await ws.send(json.dumps({"type": "response.create"}))
        if sender: await sender
        ok = "-" if check is None else ("PASS" if check(CHECKLIST) else "FAIL")
        print(f"{ok} [{time.perf_counter()-t0:.1f}s] {utt}\n   heard: {heard}\n   calls: {json.dumps(calls, ensure_ascii=False)[:300]}\n   said: {said.strip()[:220]}", flush=True)
    print("FINAL:", json.dumps([(r['zh'], r['en'], r['count'], r['notes']) for r in CHECKLIST], ensure_ascii=False))
    await ws.close()

if __name__ == "__main__":
    for m in sys.argv[1:] or ["text"]: asyncio.run(run(m))

async def run_id(mode, marks=True):
    import voice_checklist_probe as P
    if not marks:
        for g in list(P._nearby_cache): pass
    ok = 0
    for utt, want in CASES_ID:
        CHECKLIST.clear()
        sess = {"output_modalities": ["text"], "instructions": INSTR + " Base identifications on the field marks returned by nearby_species.", "tools": TOOLS}
        ws = await connect(sess); await collect(ws, until=("session.updated",), timeout=15)
        await say_text(ws, utt); said = ""
        for _ in range(4):
            r = await collect(ws, timeout=45); said += r["text"]
            cc = [x for x in r["calls"] if x.get("executor", "client") == "client"]
            if not cc: break
            for x in cc:
                res = tool(x["name"], json.loads(x["arguments"] or "{}"))
                await ws.send(json.dumps({"type": "conversation.item.create", "item": {"type": "function_call_output", "call_id": x["call_id"], "output": json.dumps(res, ensure_ascii=False)}}))
            await ws.send(json.dumps({"type": "response.create"}))
        names = [x for x in nearby_species("Aves") if x["sci"] == want]
        hit = bool(names) and any(k and k.lower() in said.lower() for k in [names[0]["en"], names[0]["zh"], want])
        ok += hit; print(("HIT " if hit else "MISS"), want, "|", said.strip()[:160], flush=True)
        await ws.close()
    print(f"ID score {ok}/{len(CASES_ID)}")
