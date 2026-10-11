"""Gemini wording: the checker and the hook into generate_reports. No map data or API key needed."""
import pathlib, sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from sim.report_writer import check, _payload
from sim.reports import apply_writer

RESCUE = {'kind': 'rescue', 'channel': 'emergency_call_119', 'lang': 'ja', 'match_type': 'misleading', 'is_repost': False,
          'place_said_en': 'in Kabukicho 2-chome', 'place_said_ja': '新宿区歌舞伎町二丁目で', 'location_precision': 'address',
          'wrong_place': True, 'building_en': '9-storey office building', 'building_ja': '9階建てのオフィスビル',
          'street_en': None, 'street_ja': None, 'people': {'condition': 'trapped', 'count_said': 16, 'count_true': 27}}


def test_checker_keeps_the_facts():
    ja = '歌舞伎町二丁目で9階建てのオフィスビルが崩れました！16人くらい閉じ込められてます、早く来てください！'
    en = 'A 9-storey office building collapsed in Kabukicho 2-chome! About 16 people are trapped, please hurry!'
    assert check(RESCUE, ja, en) is None
    assert check(RESCUE, ja.replace('16', '27'), en.replace('16', '27')) is not None           # the true count leaks
    assert check(RESCUE, ja, en.replace('Kabukicho 2-chome', 'Kabukicho')) == 'place missing (en)'
    assert check(RESCUE, ja, en + ' There is a fire on 3 floors.') is not None                 # an added number
    assert check(RESCUE, ja, ja) is not None                                                    # text_en not English


def test_truth_is_never_sent_to_gemini():
    p = _payload(0, RESCUE)
    assert p['people'] == {'condition': 'trapped', 'count': 16}
    assert not {'match_type', 'wrong_place', 'count_true', 'links'} & set(p)
    assert '27' not in str(p)


def _item(i, lang='en', repost=False):
    t = f'template {i}'
    return {'observation': {'id': str(i), 'original_text': ('RT ' if repost else '') + t, 'text_en': ('RT ' if repost else '') + t},
            'links': [], 'recipe': {'lang': lang, 'is_repost': repost}}


def test_writer_rewords_and_reposts_follow_their_original():
    a, b, c = _item(0), _item(1), _item(2)
    rt = _item(0, repost=True)
    seen = []

    def writer(recipes):
        seen.extend(recipes)
        return [{'original_text': 'new 0', 'text_en': 'new 0'}, None, {'original_text': 'new 2', 'text_en': 'new 2'}]

    assert apply_writer([a, rt, b, c], [(rt, a)], writer) == 2
    assert len(seen) == 3 and not any(r['is_repost'] for r in seen)       # reposts are not sent
    assert a['observation']['text_en'] == 'new 0' and rt['observation']['text_en'] == 'RT new 0'
    assert b['observation']['text_en'] == 'template 1'                     # rejected -> template kept


class _ApiErr(Exception):
    def __init__(self, code, text):
        super().__init__(text)
        self.code = code


class _FakeClient:
    """Stands in for genai.Client: answers each batch from a script of errors, then echoes good reports."""

    def __init__(self, errors=()):
        self.errors, self.calls = list(errors), 0
        self.models = self

    def generate_content(self, model, contents, config):
        import json
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        rows = [{'i': p['i'], 'original_text': f"Road blocked {p['place_en']}.", 'text_en': f"Road blocked {p['place_en']}."}
                for p in json.loads(contents)]
        return type('Resp', (), {'text': json.dumps(rows)})()


ROAD = {'kind': 'road_blocked', 'channel': 'eyewitness_text', 'lang': 'en', 'is_repost': False,
        'place_said_en': 'near Shinjuku Station', 'place_said_ja': '新宿駅の近くで', 'people': None}


def _fake_clock(monkeypatch):
    """time.monotonic/time.sleep for the writer: sleeping moves a fake clock, so tests never really wait."""
    import sim.report_writer as w
    clock = [1000.0]
    monkeypatch.setattr(w.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(w.time, 'sleep', lambda s: clock.__setitem__(0, clock[0] + s))
    return clock


def _run_writer(monkeypatch, tmp_path, client, n, recipe=ROAD):
    import sim.report_writer as w
    monkeypatch.setattr(w, '_client', lambda: client)
    monkeypatch.setattr(w, 'WORKERS', 1)
    monkeypatch.setattr(w, 'CACHE_PATH', str(tmp_path / 'wording.json'))   # never touch the real saved wording
    clock = _fake_clock(monkeypatch)
    start = clock[0]
    out = w.write_reports([dict(recipe) for _ in range(n)])
    return out, clock[0] - start


def test_rate_limit_waits_the_delay_google_asks_for_then_succeeds(monkeypatch, tmp_path):
    err = _ApiErr(429, "429 RESOURCE_EXHAUSTED. {'error': {'message': 'Quota exceeded. Please retry in 9.8s.'}}")
    out, waited = _run_writer(monkeypatch, tmp_path, _FakeClient([err]), 3)
    assert all(o and o['text_en'] == 'Road blocked near Shinjuku Station.' for o in out)
    assert waited >= 10          # waited Google's 9.8 s plus a margin before retrying


def test_daily_quota_stops_at_once_instead_of_waiting_hours(monkeypatch, tmp_path):
    import sim.report_writer as w
    err = _ApiErr(429, "429 RESOURCE_EXHAUSTED. {'error': {'message': 'Quota exceeded. Please retry in 67745s.'}}")
    client = _FakeClient([err] * 10)
    out, waited = _run_writer(monkeypatch, tmp_path, client, 3 * w.BATCH_SIZE)
    assert out == [None] * (3 * w.BATCH_SIZE) and client.calls == 1 and waited < 60


def test_bad_model_stops_after_one_call_and_keeps_templates(monkeypatch, tmp_path):
    import sim.report_writer as w
    client = _FakeClient([_ApiErr(404, "404 NOT_FOUND. {'error': {'message': 'model is no longer available'}}")] * 10)
    out, _ = _run_writer(monkeypatch, tmp_path, client, 3 * w.BATCH_SIZE)
    assert out == [None] * (3 * w.BATCH_SIZE) and client.calls == 1   # no point retrying or trying other batches


def test_time_budget_ends_gemini_and_keeps_templates_for_the_rest(monkeypatch, tmp_path):
    import sim.report_writer as w
    monkeypatch.setattr(w, 'BUDGET_S', 30)               # 5/min = one call every 12 s: calls at 0, 12, 24 s
    client = _FakeClient()
    out, waited = _run_writer(monkeypatch, tmp_path, client, 10 * w.BATCH_SIZE)
    assert client.calls == 3 and waited <= 30
    assert sum(o is not None for o in out) == 3 * w.BATCH_SIZE


def test_saved_wording_is_reused_without_calling_gemini(monkeypatch, tmp_path):
    first, _ = _run_writer(monkeypatch, tmp_path, _FakeClient(), 3)
    again = _FakeClient([_ApiErr(404, 'should not be called')])
    second, _ = _run_writer(monkeypatch, tmp_path, again, 3)
    assert second == first and again.calls == 0       # same words on replay, no quota spent


def test_pacer_spaces_calls_to_fit_the_per_minute_limit(monkeypatch):
    import sim.report_writer as w, threading
    clock = _fake_clock(monkeypatch)
    p, starts = w._Pacer(5, clock[0] + 3600, threading.Event()), []
    for _ in range(6):
        assert p.wait()
        starts.append(clock[0] - 1000)
    assert starts == [0, 12, 24, 36, 48, 60]     # 5 per minute: one every 12 s


def test_writer_failure_keeps_every_template():
    a = _item(0)
    def boom(recipes):
        raise RuntimeError('504')
    assert apply_writer([a], [], boom) == 0 and a['observation']['text_en'] == 'template 0'
    assert apply_writer([a], [], lambda r: []) == 0 and a['observation']['text_en'] == 'template 0'   # wrong length
