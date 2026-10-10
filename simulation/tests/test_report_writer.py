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


def test_writer_failure_keeps_every_template():
    a = _item(0)
    def boom(recipes):
        raise RuntimeError('504')
    assert apply_writer([a], [], boom) == 0 and a['observation']['text_en'] == 'template 0'
    assert apply_writer([a], [], lambda r: []) == 0 and a['observation']['text_en'] == 'template 0'   # wrong length
