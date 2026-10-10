"""Gemini report writer: turns simulator recipes into original_text / text_en.

Code decides every fact; Gemini only chooses the words. Anything Gemini returns that changes or adds a
fact is rejected by check(), and that report returns None so the simulator keeps its template text.

    pip install google-genai
    export GEMINI_API_KEY=...            # only needed for live calls
    export GEMINI_MODEL=gemini-2.5-flash # change to whichever Gemini model your key can use

In the simulator (opt-in; without --gemini, or if Gemini fails, reports keep their template text):
    python -m sim.run --seed 42 --gemini [--save-reports runs/reports.json]

Standalone, on a file in the shape of Gerard's sample (or one written by sim.run --save-reports):
    python sim/report_writer.py check reports_seed42_mw7.4.json       # offline: do the template texts pass?
    python sim/report_writer.py run   reports_seed42_mw7.4.json 40    # live: rewrite ~40 reports, show results
"""
import json
import os
import re
import sys
import unicodedata
from concurrent.futures import ThreadPoolExecutor


MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
BATCH_SIZE = 12        # recipes per Gemini call
WORKERS = 4            # calls in flight at once
TIMEOUT_MS = 60_000    # per call
TEMPERATURE = 1.0      # wording variety; check() catches factual drift

SYSTEM = """You write short reports that people send during a magnitude 7.4 earthquake in Shinjuku, Tokyo.
A simulator has already decided every fact. You only choose the words.

You receive a JSON list. For each item write one report. Return a JSON list of
{"i", "original_text", "text_en"}, using the same "i" values.
- "original_text" is in the item's "lang" ("en" or "ja"). For "ja" write natural spoken Japanese first.
- "text_en" says exactly the same thing in English. For "en" the two strings are identical. For "ja" it is a
  faithful translation of your Japanese.

FACTS YOU MUST COPY EXACTLY
- Place: place_en / place_ja is how this reporter describes where. Fit it into a sentence but give nothing more
  precise. "right where I am" / "現在地" means the reporter is standing at the spot.
- Building: building_en / building_ja is how the reporter describes the building. Use it, keeping any number in
  it. If there is no building field, name no building.
- People: people.condition is trapped, critical or injured. people.count is the head count this reporter gives:
  write it as digits ("about 16", "16人"). If count is null, give NO number and no estimate (no "a few",
  "dozens", "several"). If there is no people field, mention no casualties and no numbers.
- Street: use street_en / street_ja if present. If absent, name no street.
- Never add facts: no extra casualties, fire, second building, times, or other numbers (the emergency numbers
  119 and 110 are fine). Never hint that a report might be mistaken. A wrong report must sound as confident as a
  right one.

WHAT EACH KIND SAYS
rescue: a building collapsed and people are trapped or critically hurt. damage: a building is badly damaged,
maybe with injured people. collapse: a building collapsed, casualties unknown. minor: broken glass, fallen
tiles, nothing serious. road_blocked: a road is impassable. road_limited: a road is partly obstructed but
passable. road_open: the reporter says the road is clear. no_collapse: the reporter says a building people
mention is still standing. ambient: general shaken chatter, no specific event.
rumor_collapse / rumor_fire / rumor_road: hearsay about something that did not happen. For these you may
invent details freely, but keep the place as given.

STYLE BY CHANNEL
- emergency_call_119: someone speaking under stress to a dispatcher. Spoken, urgent, sometimes broken off.
- eyewitness_text: short, typed in a hurry. Fragments, typos and panic are fine.
- social_media: slang, hashtags, repetition, typos. Casual Japanese for ja.
- fire_police_radio: terse and procedural, like a unit reporting. No typos, no panic.
Vary length, tone and vocabulary between items. Do not start every report the same way.

IF AN ITEM HAS "thread"
"earlier" are this same person's previous messages and "changed" is what is new. Write their NEXT message:
same voice, builds on what they said, reflects the change, does not repeat earlier lines."""


def _out_schema():
    from pydantic import BaseModel   # installed with google-genai

    class Out(BaseModel):
        i: int
        original_text: str
        text_en: str
    return list[Out]


# ---------------------------------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------------------------------

JA = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff]")
NUM_WORDS = re.compile(r"\b(two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|twenty|thirty|"
                       r"dozens?|hundreds?|thousands?)\b")
KANJI_COUNT = re.compile(r"[一二三四五六七八九十百千万〇]+(?:人|名)")


def _n(s):
    """Normalise for comparing: full-width to ASCII, strip Latin accents, case-fold."""
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not 0x300 <= ord(c) <= 0x36F).casefold()


def _digits(s):
    return re.findall(r"\d+", _n(s))


def _core_en(place):
    if place.strip().lower() == "right where i am":
        return None
    return re.sub(r"^(in|near|somewhere around)\s+", "", place, flags=re.I)


def _core_ja(place):
    if place == "現在地で":
        return None
    core = re.sub(r"(の近くで|のあたりで|で)$", "", place)
    # Speakers drop the ward on an address (新宿区歌舞伎町二丁目 -> 歌舞伎町二丁目): same block, same precision.
    return re.sub(r"^\S+?区(?=\S+(丁目|町)$)", "", core)


def check(r, original_text, text_en):
    """-> None if the text keeps every fact in recipe r, else a short reason string."""
    ot, en = (original_text or "").strip(), (text_en or "").strip()
    lang = r["lang"]
    if not ot or not en:
        return "empty"
    if len(ot) > 600 or len(en) > 600:
        return "too long"
    if lang == "en":
        if ot != en:
            return "en texts differ"
        if JA.search(ot):
            return "Japanese in an English report"
    else:
        if not JA.search(ot):
            return "no Japanese in a ja report"
        if JA.search(en):
            return "Japanese in text_en"

    # Place and street must appear as given.
    core = _core_en(r["place_said_en"])
    if core and _n(core) not in _n(en):
        return "place missing (en)"
    if lang == "ja":
        core = _core_ja(r["place_said_ja"])
        if core and _n(core) not in _n(ot):
            return "place missing (ja)"
    if r.get("street_en") and _n(r["street_en"]) not in _n(en):
        return "street missing (en)"
    if lang == "ja" and r.get("street_ja") and _n(r["street_ja"]) not in _n(ot):
        return "street missing (ja)"

    if r["kind"].startswith("rumor"):
        return None   # rumours may invent details

    # No numbers except the ones the recipe allows.
    fixed_en = [r.get(k) for k in ("place_said_en", "building_en", "street_en")]
    fixed_ja = [r.get(k) for k in ("place_said_ja", "building_ja", "street_ja")]
    ppl = r.get("people")
    count = ppl.get("count_said") if ppl else None
    allowed = {"110", "119"}
    for s in fixed_en + fixed_ja:
        if s:
            allowed.update(_digits(s))
    if count is not None:
        allowed.add(str(count))
    th = r.get("thread")
    if th:
        for s in [th.get("changed") or ""] + [m.get("original_text", "") + m.get("text_en", "") for m in th.get("earlier", [])]:
            allowed.update(_digits(s))

    scans = [("en", en, fixed_en)]
    if lang == "ja":
        scans.append(("ja", ot, fixed_ja))
    leftover = {}
    for name, text, fixed in scans:
        t = _n(text)
        for s in fixed:
            if s:
                t = t.replace(_n(s), " ")
        leftover[name] = re.findall(r"\d+", t)
        for d in leftover[name]:
            if d not in allowed:
                return f"unexpected number {d}"
        if NUM_WORDS.search(t):
            return "number written as a word"
        if KANJI_COUNT.search(t):
            return "count written in kanji"

    # The head count must be present when one was given.
    if count is not None and count > 1:
        for name in leftover:
            if str(count) not in leftover[name]:
                return f"count missing ({name})"
    return None


# ---------------------------------------------------------------------------------------------------
# Gemini calls
# ---------------------------------------------------------------------------------------------------

def _payload(i, r):
    """Only what Gemini needs. count_true, match_type, wrong_place and links are never sent."""
    p = {"i": i, "kind": r["kind"], "channel": r["channel"], "lang": r["lang"],
         "place_en": r["place_said_en"], "place_ja": r["place_said_ja"]}
    if r.get("building_en"):
        p["building_en"], p["building_ja"] = r["building_en"], r.get("building_ja")
    if r.get("street_en") or r.get("street_ja"):
        p["street_en"], p["street_ja"] = r.get("street_en"), r.get("street_ja")
    if r.get("people"):
        p["people"] = {"condition": r["people"]["condition"], "count": r["people"]["count_said"]}
    if r.get("thread"):
        th = r["thread"]
        p["thread"] = {"earlier": [{"sim_time_s": m.get("sim_time_s"), "original_text": m.get("original_text"),
                                    "text_en": m.get("text_en")} for m in th.get("earlier", [])],
                       "changed": th.get("changed")}
    return p


def _client():
    from google import genai
    from google.genai import types
    return genai.Client(http_options=types.HttpOptions(timeout=TIMEOUT_MS))


def _call(client, recipes, idxs):
    """One Gemini call for a batch -> {i: {"original_text", "text_en"}}; {} if it failed."""
    from google.genai import types
    wanted = set(idxs)
    cfg = types.GenerateContentConfig(system_instruction=SYSTEM, temperature=TEMPERATURE,
                                      response_mime_type="application/json", response_schema=_out_schema())
    contents = json.dumps([_payload(i, recipes[i]) for i in idxs], ensure_ascii=False)
    for attempt in range(2):
        try:
            resp = client.models.generate_content(model=MODEL, contents=contents, config=cfg)
            rows = json.loads(resp.text)
            return {int(x["i"]): x for x in rows if int(x["i"]) in wanted}
        except Exception as e:   # slow, rate-limited, bad JSON: try once more, then give up on the batch
            print(f"[report_writer] batch failed (attempt {attempt + 1}): {e}", file=sys.stderr)
    return {}


def write_reports(recipes):
    """One {'original_text', 'text_en'} per recipe, in order; None = keep the simulator's template text."""
    results = [None] * len(recipes)
    # Reposts stay with the simulator (it copies the original's text with an "RT " prefix).
    todo = [i for i, r in enumerate(recipes) if not r.get("is_repost")]
    batches = [todo[k:k + BATCH_SIZE] for k in range(0, len(todo), BATCH_SIZE)]
    try:
        client = _client()
    except Exception as e:
        print(f"[report_writer] no Gemini client: {e}", file=sys.stderr)
        return results

    rejected = {}
    with ThreadPoolExecutor(WORKERS) as ex:
        for got in ex.map(lambda b: _call(client, recipes, b), batches):
            for i, row in got.items():
                ot = (row.get("original_text") or "").strip()
                en = (row.get("text_en") or "").strip()
                if recipes[i]["lang"] == "en":
                    en = ot
                why = check(recipes[i], ot, en)
                if why:
                    rejected[why] = rejected.get(why, 0) + 1
                else:
                    results[i] = {"original_text": ot, "text_en": en}
    if rejected:
        print(f"[report_writer] rejected: {rejected}", file=sys.stderr)
    return results


# ---------------------------------------------------------------------------------------------------
# Test CLI (reads Gerard's sample file; no simulator needed)
# ---------------------------------------------------------------------------------------------------

def _load(path):
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    return d["reports"] if isinstance(d, dict) else d


def _cmd_check(items):
    """Offline sanity test: the simulator's own template texts should pass the checker."""
    bad, n = {}, 0
    for it in items:
        if it["recipe"]["is_repost"]:
            continue
        n += 1
        why = check(it["recipe"], it["observation"]["original_text"], it["observation"]["text_en"])
        if why:
            bad.setdefault(why, []).append(it)
    print(f"{n - sum(len(v) for v in bad.values())}/{n} template texts pass")
    for why, lst in bad.items():
        print(f"\n{why}: {len(lst)}")
        for it in lst[:2]:
            print("   ", it["recipe"]["kind"], it["recipe"]["lang"], "|", it["observation"]["original_text"])


def _cmd_run(items, n):
    items = [it for it in items if not it["recipe"]["is_repost"]]
    items = items[::max(1, len(items) // n)][:n]
    out = write_reports([it["recipe"] for it in items])
    ok = [(it, o) for it, o in zip(items, out) if o]
    print(f"{len(ok)}/{len(items)} reports accepted")
    for it, o in ok[:10]:
        r = it["recipe"]
        print(f"\n[{r['kind']} / {r['channel']} / {r['lang']}]")
        print("  template:", it["observation"]["original_text"])
        print("  gemini:  ", o["original_text"])
        if r["lang"] == "ja":
            print("  english: ", o["text_en"])
    with open("rewritten_sample.json", "w", encoding="utf-8") as f:
        json.dump([{"id": it["observation"]["id"], **o} for it, o in ok], f, ensure_ascii=False, indent=1)
    print("\nsaved rewritten_sample.json")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")   # Windows consoles default to cp1252 and crash on Japanese
    if len(sys.argv) < 3 or sys.argv[1] not in ("check", "run"):
        sys.exit(__doc__)
    data = _load(sys.argv[2])
    if sys.argv[1] == "check":
        _cmd_check(data)
    else:
        _cmd_run(data, int(sys.argv[3]) if len(sys.argv) > 3 else 40)