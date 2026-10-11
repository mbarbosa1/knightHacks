"""Gemini report writer: turns simulator recipes into original_text / text_en.

Code decides every fact; Gemini only chooses the words. Anything Gemini returns that changes or adds a
fact is rejected by check(), and that report returns None so the simulator keeps its template text.

    pip install google-genai
    export GEMINI_API_KEY=...            # only needed for live calls
    export GEMINI_MODEL=gemini-3.8-flash # optional; whichever Gemini model your key can use
    export GEMINI_RPM=5                  # optional; requests per minute your key allows (free tier: 5)

    export GEMINI_BUDGET_S=180           # optional; seconds Gemini gets per run, then the rest keep templates

Made for the free tier: calls are paced under GEMINI_RPM, Gemini stops at once when the daily quota is
used up or the time budget runs out (those reports keep their template text), and accepted wording is
saved in simulation/cache/gemini_wording.json and reused, so re-running a seed costs no quota and gives the
same words. Each run fills in more of what is missing. Raise GEMINI_RPM on a paid key to go faster.

In the simulator (opt-in; without --gemini, or if Gemini fails, reports keep their template text):
    python -m sim.run --seed 42 --gemini [--save-reports runs/reports.json]

Standalone, on a file in the shape of Gerard's sample (or one written by sim.run --save-reports):
    python sim/report_writer.py models                                # models your key can use (pick GEMINI_MODEL)
    python sim/report_writer.py check reports_seed42_mw7.4.json       # offline: do the template texts pass?
    python sim/report_writer.py run   reports_seed42_mw7.4.json 40    # live: rewrite ~40 reports, show results
"""
import hashlib
import json
import os
import re
import sys
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash").strip()   # strip: a stray space gives a 400
RPM = float(os.environ.get("GEMINI_RPM", "5"))   # requests per minute the key allows; free tier is 5
BUDGET_S = float(os.environ.get("GEMINI_BUDGET_S", "180"))   # Gemini's time per run; after it, templates
BATCH_SIZE = 60        # recipes per Gemini call: few calls suit the free tier's per-minute and daily limits
WORKERS = 2            # calls in flight at once; the pacer still keeps starts under RPM
TIMEOUT_MS = 120_000   # per call; a 60-report batch takes Gemini a while to write
ATTEMPTS = 2           # per batch: one retry for a busy or slow moment; more would just spend the daily quota
MAX_WAIT_S = 60        # a 429 asking for a longer wait is the daily quota, not the per-minute one: stop
TEMPERATURE = 1.0      # wording variety; check() catches factual drift
# Accepted wording is saved here and reused (simulation/cache/ is gitignored). GEMINI_CACHE=off disables it.
_cache_env = os.environ.get("GEMINI_CACHE", "")
CACHE_PATH = None if _cache_env.lower() == "off" else (
    _cache_env or str(Path(__file__).resolve().parent.parent / "cache" / "gemini_wording.json"))

SYSTEM = """You write short reports that people send during a major earthquake in Shinjuku, Tokyo.
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


class _Pacer:
    """Spaces call starts at least 60/rpm seconds apart across threads, so the per-minute limit holds,
    and refuses to start anything after the deadline (the run's time budget for Gemini)."""

    def __init__(self, rpm, deadline, stop):
        self.gap = 60.0 / rpm if rpm > 0 else 0.0
        self.next, self.deadline, self.stop = 0.0, deadline, stop
        self.lock = threading.Lock()

    def wait(self, extra=0.0):
        """Block until this caller may start a call; False if the budget ran out or the run was stopped.
        extra pushes every later call back too (a 429's retry delay)."""
        with self.lock:
            now = time.monotonic()
            if extra:
                self.next = max(self.next, now + extra)
            start = max(now, self.next)
            if start > self.deadline:
                return False
            self.next = start + self.gap
        while not self.stop.is_set():          # short naps so Ctrl+C or a fatal error ends the wait at once
            left = start - time.monotonic()
            if left <= 0:
                break
            time.sleep(min(left, 0.5))
        return not self.stop.is_set()


# Errors worth one more try (rate limit, busy, timeout) vs ones a retry cannot fix (bad model, bad key).
_FATAL = {400, 401, 403, 404}


def _error_code(e):
    code = getattr(e, "code", None)
    if isinstance(code, int):
        return code
    m = re.match(r"\s*(\d{3})\b", str(e))
    return int(m.group(1)) if m else None


def _retry_delay(e):
    """Seconds Google asked us to wait (in a 429), or None."""
    m = re.search(r"retry in ([\d.]+)\s*s", str(e), re.I) or re.search(r"retryDelay'?\"?:\s*'?\"?(\d+)s", str(e))
    return float(m.group(1)) if m else None


def _daily_quota_gone(e, delay):
    """A 429 for the per-day quota (or one asking us to wait longer than any run would) cannot be waited out."""
    return "PerDay" in str(e) or (delay is not None and delay > MAX_WAIT_S)


def _short(e):
    """One readable line instead of the whole JSON error."""
    m = re.search(r"'message':\s*'([^'\\]*)", str(e))
    msg = m.group(1) if m else str(e)
    return msg.split("\n")[0][:160]


def _halt(stop, why):
    if not stop.is_set():
        stop.set()
        print(f"[report_writer] stopping Gemini, the rest keep template text: {why}", file=sys.stderr)


def _call(client, recipes, idxs, pacer, stop):
    """One Gemini batch -> {i: {"original_text", "text_en"}}; {} if it gave up. Sets stop when Gemini cannot help."""
    from google.genai import types
    wanted = set(idxs)
    cfg = types.GenerateContentConfig(system_instruction=SYSTEM, temperature=TEMPERATURE,
                                      response_mime_type="application/json", response_schema=_out_schema())
    contents = json.dumps([_payload(i, recipes[i]) for i in idxs], ensure_ascii=False)
    extra = 0.0
    for attempt in range(1, ATTEMPTS + 1):
        if stop.is_set() or not pacer.wait(extra):
            return {}
        try:
            resp = client.models.generate_content(model=MODEL, contents=contents, config=cfg)
            rows = json.loads(resp.text)
            return {int(x["i"]): x for x in rows if int(x["i"]) in wanted}
        except Exception as e:
            code, delay = _error_code(e), _retry_delay(e)
            if code in _FATAL:
                _halt(stop, f"Gemini said {code}: {_short(e)} (model {MODEL}; check GEMINI_MODEL / GEMINI_API_KEY)")
                return {}
            if code == 429 and _daily_quota_gone(e, delay):
                hours = f", resets in about {delay / 3600:.0f} h" if delay else ""
                _halt(stop, f"the key's daily Gemini quota is used up{hours}. Saved wording is still used.")
                return {}
            extra = (delay + 1.0) if delay else 5.0
            more = f", retrying in ~{extra:.0f}s" if attempt < ATTEMPTS else ", giving up on this batch"
            print(f"[report_writer] batch error {code or ''} {_short(e)}{more}", file=sys.stderr)
    return {}


# ---------------------------------------------------------------------------------------------------
# Saved wording: accepted text is kept and reused, so the same report is never paid for twice (the free
# tier has a daily quota) and replaying a seed gives the same words.
# ---------------------------------------------------------------------------------------------------

def _cache_keys(recipes, idxs):
    """Key = what Gemini is shown + how many identical reports came before it in this run, so two reporters
    with the same facts still get different wording, and the nth one gets the same wording on every replay."""
    seen, keys = {}, {}
    for i in idxs:
        s = json.dumps(_payload(0, recipes[i]), sort_keys=True, ensure_ascii=False)
        n = seen.get(s, 0)
        seen[s] = n + 1
        keys[i] = hashlib.sha1(f"{s}#{n}".encode("utf-8")).hexdigest()
    return keys


def _load_cache():
    if not CACHE_PATH:
        return {}
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_cache(cache):
    if not CACHE_PATH:
        return
    path = Path(CACHE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)
    os.replace(tmp, path)


def write_reports(recipes):
    """One {'original_text', 'text_en'} per recipe, in order; None = keep the simulator's template text."""
    results = [None] * len(recipes)
    # Reposts stay with the simulator (it copies the original's text with an "RT " prefix).
    todo = [i for i, r in enumerate(recipes) if not r.get("is_repost")]
    keys, cache = _cache_keys(recipes, todo), _load_cache()
    hits = 0
    for i in todo:
        hit = cache.get(keys[i])
        if hit and not check(recipes[i], hit["original_text"], hit["text_en"]):
            results[i] = hit
            hits += 1
    todo = [i for i in todo if results[i] is None]
    batches = [todo[k:k + BATCH_SIZE] for k in range(0, len(todo), BATCH_SIZE)]
    if hits:
        print(f"[report_writer] {hits} reports reuse saved wording", file=sys.stderr)
    if not batches:
        return results
    try:
        client = _client()
    except Exception as e:
        print(f"[report_writer] no Gemini client: {e}", file=sys.stderr)
        return results

    print(f"[report_writer] {len(todo)} reports in {len(batches)} calls to {MODEL} at up to {RPM:g}/min "
          f"(~{len(batches) / RPM:.1f} min; Gemini gets at most {BUDGET_S:.0f}s, then templates)", file=sys.stderr)
    stop = threading.Event()
    pacer = _Pacer(RPM, time.monotonic() + BUDGET_S, stop)
    rejected, done, written = {}, 0, 0
    ex = ThreadPoolExecutor(WORKERS)
    try:
        for got in ex.map(lambda b: _call(client, recipes, b, pacer, stop), batches):
            done += 1
            for i, row in got.items():
                ot = (row.get("original_text") or "").strip()
                en = (row.get("text_en") or "").strip()
                if recipes[i]["lang"] == "en":
                    en = ot
                why = check(recipes[i], ot, en)
                if why:
                    rejected[why] = rejected.get(why, 0) + 1
                else:
                    results[i] = cache[keys[i]] = {"original_text": ot, "text_en": en}
                    written += 1
            if got:
                _save_cache(cache)   # after every batch, so a stopped run keeps what it already paid for
            if not stop.is_set():
                print(f"[report_writer] call {done}/{len(batches)}: {len(got)} reports back", file=sys.stderr)
    except KeyboardInterrupt:
        stop.set()                   # wakes waiting calls instead of sitting on them while the user waits
        ex.shutdown(wait=False, cancel_futures=True)
        raise
    ex.shutdown(wait=True)
    if not stop.is_set() and written < len(todo) and time.monotonic() > pacer.deadline:
        print(f"[report_writer] Gemini's {BUDGET_S:.0f}s budget ran out; the rest keep template text "
              f"(set GEMINI_BUDGET_S to allow more)", file=sys.stderr)
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


def _cmd_models():
    """Models this key can write text with; each has its own free-tier quota (ai.dev/rate-limit shows them)."""
    client = _client()   # keep a reference: the list is fetched page by page, and a dropped client closes itself
    for m in client.models.list():
        if "generateContent" in (getattr(m, "supported_actions", None) or []):
            print(m.name.removeprefix("models/"), "  <- current" if m.name.endswith("/" + MODEL) else "")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")   # Windows consoles default to cp1252 and crash on Japanese
    if len(sys.argv) == 2 and sys.argv[1] == "models":
        sys.exit(_cmd_models())
    if len(sys.argv) < 3 or sys.argv[1] not in ("check", "run"):
        sys.exit(__doc__)
    data = _load(sys.argv[2])
    if sys.argv[1] == "check":
        _cmd_check(data)
    else:
        _cmd_run(data, int(sys.argv[3]) if len(sys.argv) > 3 else 40)