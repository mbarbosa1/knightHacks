"""Hidden truth -> the noisy reports agents see, plus the answer key (truth_links) for scoring.

Code decides every fact here: who reports what, when, how wrong and from where. The wording comes from
templates; Gemini can reword it later without changing a fact. Channel settings, report counts and noise
levels are our own assumptions (UNCERTAIN, tunable). The Japanese templates have not been checked by a
native speaker.
"""
import math, random, re, uuid
from .scenario import CENTER
from .roads import KX, KY

# delay: seconds after the earthquake (min, max). count_noise: sigma of the log error on head counts.
CHANNELS = {
    'emergency_call_119': {'style': 'call', 'delay': (30, 600), 'reliability': (0.60, 0.85), 'count_noise': 0.3, 'count_stated': 0.8,
                           'precision': {'address': 0.6, 'landmark': 0.3, 'vague': 0.1}, 'lang': {'en': 0.6, 'ja': 0.4}},
    'eyewitness_text':    {'style': 'text', 'delay': (60, 900), 'reliability': (0.50, 0.75), 'count_noise': 0.5, 'count_stated': 0.6,
                           'precision': {'gps': 0.5, 'address': 0.2, 'landmark': 0.2, 'vague': 0.1}, 'lang': {'en': 0.6, 'ja': 0.4}},
    'social_media':       {'style': 'social', 'delay': (20, 1200), 'reliability': (0.20, 0.50), 'count_noise': 1.0, 'count_stated': 0.4,
                           'precision': {'gps': 0.2, 'landmark': 0.4, 'vague': 0.4}, 'lang': {'en': 0.6, 'ja': 0.4}},
    'fire_police_radio':  {'style': 'radio', 'delay': (180, 1500), 'reliability': (0.85, 0.95), 'count_noise': 0.15, 'count_stated': 0.9,
                           'precision': {'address': 0.7, 'gps': 0.3}, 'lang': {'ja': 0.8, 'en': 0.2}},
}
# Which channel carries which kind of event.
CHANNEL_MIX = {
    'rescue': {'emergency_call_119': 0.45, 'eyewitness_text': 0.2, 'social_media': 0.2, 'fire_police_radio': 0.15},
    'damage': {'social_media': 0.5, 'eyewitness_text': 0.3, 'emergency_call_119': 0.1, 'fire_police_radio': 0.1},
    'road':   {'fire_police_radio': 0.35, 'social_media': 0.3, 'eyewitness_text': 0.3, 'emergency_call_119': 0.05},
    'rumor':  {'social_media': 0.75, 'eyewitness_text': 0.2, 'emergency_call_119': 0.05},
    'ambient': {'social_media': 0.8, 'eyewitness_text': 0.2},
}
# (chance nobody reports it at all, mean number of extra reports beyond the first)
COVERAGE = {'rescue': (0.10, 2.0), 'collapsed': (0.15, 1.0), 'severe': (0.50, 0.3), 'moderate': (0.85, 0.0), 'light': (0.98, 0.0),
            'BLOCKED': (0.40, 0.5), 'LIMITED': (0.65, 0.0)}
GEOM_ERROR_M = {'gps': 12, 'address': 40}   # sigma; an 'address' report stands for a street address our data cannot spell out
WRONG_PLACE = 0.08          # share of reports that put the event 150-400 m away
CONTRADICTION = 0.06        # share of road / collapse reports that say the opposite
RUMOR_SHARE = 0.06          # fabricated reports, as a share of real ones
AMBIENT_SHARE = 0.05        # chatter with no event behind it
REPOST = 0.15               # chance a social post is reposted later

BUILDING_WORD = {'office': ('office building', 'オフィスビル'), 'commercial': ('commercial building', '商業ビル'),
                 'mixed_commercial': ('commercial building', '商業ビル'), 'apartment': ('apartment building', 'マンション'),
                 'house': ('house', '住宅'), 'house_with_shop': ('shop-house', '店舗付き住宅'), 'hotel': ('hotel', 'ホテル'),
                 'education_welfare': ('school building', '学校の建物'), 'government': ('government building', '庁舎')}

T = {  # kind -> lang -> style -> template. '*' is the fallback style.
 'rescue': {
  'en': {'call': 'A {building} has collapsed {place}. {People}. Please send help.',
         'text': 'A {building} came down {place}. {People}. We need rescue now.',
         'social': 'Whole building just collapsed {place}!! {people}, someone call 119 #Shinjuku #earthquake',
         'radio': 'Collapse confirmed, {building} {place}. {People}. Requesting rescue team.'},
  'ja': {'call': '{place}{building}が倒壊しました。{people}。救助をお願いします。',
         'radio': '{place}{building}の倒壊を確認。{people}。救助隊を要請する。',
         '*': '{place}建物が崩れた！{people}。誰か119番して！'}},
 'collapse': {
  'en': {'radio': 'Collapse confirmed, {building} {place}. No confirmed casualties yet.',
         '*': 'A {building} has completely collapsed {place}. I cannot tell if anyone was inside.'},
  'ja': {'radio': '{place}{building}の倒壊を確認。負傷者は未確認。',
         '*': '{place}{building}が倒壊しました。中に人がいるかは分かりません。'}},
 'damage': {
  'en': {'social': 'Huge cracks in a building {place}, it looks like it could come down. {People_opt}',
         'radio': 'Heavy structural damage, {building} {place}. Building evacuated. {People_opt}',
         '*': 'A {building} {place} is badly damaged, walls cracked and leaning. People are getting out. {People_opt}'},
  'ja': {'radio': '{place}{building}に重大な損傷。建物から避難済み。{people_opt}',
         '*': '{place}{building}に大きなひびが入り、傾いています。みんな避難しています。{people_opt}'}},
 'minor': {
  'en': {'*': 'Broken windows and fallen tiles {place}, nothing serious as far as I can see.'},
  'ja': {'*': '{place}窓ガラスが割れて瓦が落ちましたが、大きな被害はなさそうです。'}},
 'road_blocked': {
  'en': {'radio': 'Road blocked by debris {street} {place}. Impassable to vehicles.',
         'social': 'The road {street} {place} is totally blocked, rubble all over it',
         '*': 'The road {street} {place} is completely blocked by rubble. Cars cannot get through.'},
  'ja': {'radio': '{place}{street}道路ががれきで通行不能。車両は通れない。',
         '*': '{place}{street}道路ががれきで完全にふさがれています。車は通れません。'}},
 'road_limited': {
  'en': {'radio': 'Partial obstruction {street} {place}. Passable with caution.',
         '*': 'There is debris on the road {street} {place}. One lane is still passable, slowly.'},
  'ja': {'radio': '{place}{street}道路に一部障害物。徐行で通行可能。',
         '*': '{place}{street}道路にがれきがあります。片側だけゆっくり通れます。'}},
 'road_open': {
  'en': {'*': 'I just drove along the road {street} {place} and it is clear.'},
  'ja': {'*': '{place}{street}道路をさっき通りましたが、問題なく通れました。'}},
 'no_collapse': {
  'en': {'*': 'People are saying a building collapsed {place} but I am standing here and everything is still up.'},
  'ja': {'*': '{place}ビルが倒れたと言われていますが、今ここにいて、建物は全部立っています。'}},
 'rumor_collapse': {
  'en': {'*': 'Hearing that a building collapsed {place} with lots of people trapped?? Can anyone confirm'},
  'ja': {'*': '{place}ビルが倒れて大勢閉じ込められてるって聞いたけど本当？誰か確認できる？'}},
 'rumor_fire': {
  'en': {'*': 'Big fire {place}, they say the smoke is everywhere'},
  'ja': {'*': '{place}大きな火事らしい。煙がすごいって。'}},
 'rumor_road': {
  'en': {'*': 'They are saying the road {street} {place} is blocked, avoid it'},
  'ja': {'*': '{place}{street}道路が通れないらしい。避けたほうがいい。'}},
 'ambient': {
  'en': {'*': 'That was the strongest shaking I have ever felt. Is everyone ok {place}? Phones are barely working.'},
  'ja': {'*': '今までで一番強い揺れだった。{place}みんな大丈夫？電話がほとんどつながらない。'}},
}


def _pick(rng, weights):
    return rng.choices(list(weights), list(weights.values()))[0]


def _poisson(rng, mean):
    if mean <= 0:
        return 0
    limit, k, p = math.exp(-mean), 0, rng.random()
    while p > limit:
        k, p = k + 1, p * rng.random()
    return k


def _offset(lon, lat, east_m, north_m):
    return lon + east_m / KX, lat + north_m / KY


def _dist_m(a, b):
    return math.hypot((a[0] - b[0]) * KX, (a[1] - b[1]) * KY)


class _Places:
    """Names a reporter could use for a spot: chome address, nearest landmark, neighbourhood."""

    def __init__(self, m):
        self.landmarks = [('Shinjuku Station', '新宿駅', CENTER)]
        self.landmarks += [(f['name_en'], f['name_ja'], (f['lon'], f['lat'])) for f in m.facilities.values()]
        self.landmarks += [(p['name_en'], p['name_ja'], (m.building_geoms[b].centroid.x, m.building_geoms[b].centroid.y))
                           for b, p in m.buildings.items() if p.get('name_en')]
        self.addressed = [((g.centroid.x, g.centroid.y), m.buildings[b]) for b, g in m.building_geoms.items()
                          if m.buildings[b].get('address_en')]

    def landmark(self, pt):
        return min(self.landmarks, key=lambda l: _dist_m(pt, l[2]))

    def address(self, pt, props=None):
        """(en, ja) chome-level address of the building, or of the nearest building that has one."""
        if not (props and props.get('address_en')):
            props = min(self.addressed[::7], key=lambda a: _dist_m(pt, a[0]))[1]
        return props['address_en'].split(',')[0], props['address_ja'].replace('東京都', '')

    def phrases(self, precision, pt, props=None):
        """-> (claimed_location as stated, English phrase, Japanese phrase, point the reporter means or None)."""
        en, ja = self.address(pt, props)
        if precision == 'gps':
            return None, 'right where I am', '現在地で', pt
        if precision == 'address':
            return en, f'in {en}', f'{ja}で', pt
        if precision == 'landmark':
            lm = self.landmark(pt)
            return f'near {lm[0]}', f'near {lm[0]}', f'{lm[1]}の近くで', lm[2]
        area_en = re.sub(r'\s*\d+-chome$', '', en)
        area_ja = re.sub(r'^.*?区', '', re.sub(r'[一二三四五六七八九十]+丁目$', '', ja))
        return f'somewhere around {area_en}', f'somewhere around {area_en}', f'{area_ja}のあたりで', None


def _building_words(props):
    if props.get('name_en'):
        return props['name_en'], props.get('name_ja') or props['name_en']
    en, ja = BUILDING_WORD.get(props.get('usage'), ('building', '建物'))
    if props.get('structure') == 'wood' and props.get('usage') in ('house', 'house_with_shop'):
        en, ja = 'wooden ' + en, '木造' + ja
    f = props.get('floors')
    return (f'{f}-storey {en}', f'{f}階建ての{ja}') if f and f >= 3 else (en, ja)


def _people_words(n, cond):
    verb_en = {'trapped': 'trapped', 'critical': 'critically hurt', 'injured': 'hurt'}[cond]
    verb_ja = {'trapped': '閉じ込められています', 'critical': '重体です', 'injured': 'けがをしています'}[cond]
    if n is None:
        return f'people are {verb_en}', f'人が{verb_ja}'
    return (f'about {n} people are {verb_en}', f'約{n}人が{verb_ja}') if n > 1 else (f'one person is {verb_en}', f'1人が{verb_ja}')


def _render(kind, lang, style, f):
    def fill(l):
        t = T[kind][l].get(style) or T[kind][l]['*']
        pe, pj = f.get('people', ('', ''))
        street = f.get('street', (None, None))
        vals = {'place': f['place'][0 if l == 'en' else 1], 'building': f.get('building', ('building', '建物'))[0 if l == 'en' else 1]}
        if l == 'en':
            vals.update(people=pe, People=pe[:1].upper() + pe[1:], People_opt=(pe[:1].upper() + pe[1:] + '.') if pe else '',
                        street=f'on {street[0]}' if street[0] else '')
        else:
            vals.update(people=pj, people_opt=(pj + '。') if pj else '', street=f'{street[1]}の' if street[1] else '')
        s = re.sub(r'\s+', ' ', t.format(**vals)).replace(' .', '.').replace(' ,', ',').strip()
        return re.sub(r'\b([Aa]) (?=(8|11|18)\b)', r'\1n ', s)   # an 8-storey, an 11-storey
    return fill(lang), fill('en')


def generate_reports(static_map, sc, truth, volume=1.0):
    """-> [{'observation': {...}, 'links': [...], 'recipe': {...}}] in release order. Deterministic for a seed.
    observation rows match ops.observations minus received_at (set when the report is released);
    links match truth_links minus scenario_id. recipe is every fact the wording was built from, for a
    writer (Gemini) that replaces the template text; it contains truth, so it never leaves the simulator.
    volume scales how many reports there are."""
    m, rng, places = static_map, random.Random(f'{sc.seed}:reports'), _Places(static_map)
    out = []

    def emit(kind, mix, pt, links, fields, props=None, match='matched', lang=None, when=None, allow_address=True, group=None):
        channel = _pick(rng, CHANNEL_MIX[mix])
        c = CHANNELS[channel]
        n = None
        if group is not None:   # head count as this channel would give it: sometimes missing, sometimes wrong
            if rng.random() < c['count_stated']:
                n = max(1, round(group['count'] * math.exp(rng.gauss(0, c['count_noise']))))
                if abs(n - group['count']) >= 3 and not 0.5 <= n / group['count'] <= 2:
                    match = 'misleading'
            fields = {**fields, 'people': _people_words(n, group['condition'])}
        precision = _pick(rng, c['precision'])
        if precision == 'address' and not allow_address:
            precision = 'landmark'
        said_pt, wrong_place = pt, False
        if match in ('matched', 'misleading') and rng.random() < WRONG_PLACE:
            a, d = rng.uniform(0, 2 * math.pi), rng.uniform(150, 400)
            said_pt, match, props, wrong_place = _offset(*pt, d * math.cos(a), d * math.sin(a)), 'misleading', None, True
        stated, en, ja, geom_pt = places.phrases(precision, said_pt, props)
        if geom_pt is not None and precision in GEOM_ERROR_M:
            geom_pt = _offset(*geom_pt, rng.gauss(0, GEOM_ERROR_M[precision]), rng.gauss(0, GEOM_ERROR_M[precision]))
        lang = lang or _pick(rng, c['lang'])
        text, text_en = _render(kind, lang, c['style'], {**fields, 'place': (en, ja)})
        obs = {'id': str(uuid.UUID(int=rng.getrandbits(128), version=4)), 'run_id': truth.run_id, 'channel': channel,
               'source_reliability': round(rng.uniform(*c['reliability']), 2), 'format': 'text', 'lang': lang,
               'original_text': text, 'text_en': text_en, 'media_url': None, 'claimed_location': stated,
               'claimed_geom': {'type': 'Point', 'coordinates': [round(geom_pt[0], 6), round(geom_pt[1], 6)]} if geom_pt else None,
               'location_precision': precision,
               'sim_time_s': round(when if when is not None else rng.uniform(*c['delay']), 1)}
        recipe = {'kind': kind, 'channel': channel, 'lang': lang, 'match_type': match, 'is_repost': False,
                  'place_said_en': en, 'place_said_ja': ja, 'location_precision': precision, 'wrong_place': wrong_place,
                  'building_en': fields['building'][0] if 'building' in fields else None,
                  'building_ja': fields['building'][1] if 'building' in fields else None,
                  'street_en': fields['street'][0] if 'street' in fields else None,
                  'street_ja': fields['street'][1] if 'street' in fields else None,
                  'people': {'condition': group['condition'], 'count_said': n, 'count_true': group['count']} if group else None}
        item = {'observation': obs, 'links': [{**l, 'match_type': match} for l in links] or
                [{'truth_entity_type': None, 'truth_entity_id': None, 'truth_simulation_time_s': None, 'match_type': match}],
                'recipe': recipe}
        out.append(item)
        if channel == 'social_media' and rng.random() < REPOST:   # the same post again: a duplicate to merge
            again = dict(obs, id=str(uuid.UUID(int=rng.getrandbits(128), version=4)),
                         original_text='RT ' + text, text_en='RT ' + text_en,
                         source_reliability=round(max(0.1, obs['source_reliability'] - 0.1), 2),
                         sim_time_s=round(obs['sim_time_s'] + rng.uniform(30, 300), 1))
            out.append({'observation': again, 'links': item['links'], 'recipe': {**recipe, 'is_repost': True}})
        return item

    def how_many(key):
        miss, extra = COVERAGE[key]
        if rng.random() < 1 - (1 - miss) * min(volume, 1.0):
            return 0
        return 1 + _poisson(rng, extra * volume)

    # --- buildings and the people in them ---------------------------------------------------------
    groups = {}
    for gid, g in truth.people.items():
        groups.setdefault(g['building_id'], {})[g['condition']] = (gid, g)
    for bid, d in truth.building_damage.items():
        level = d['damage_level']
        if level == 'none':
            continue
        props, c = m.buildings[bid], m.building_geoms[bid].centroid
        worst = next((groups.get(bid, {}).get(x) for x in ('trapped', 'critical', 'injured') if x in groups.get(bid, {})), None)
        serious = worst is not None and worst[1]['condition'] in ('trapped', 'critical')
        for _ in range(how_many('rescue' if serious else level)):
            links = [{'truth_entity_type': 'building', 'truth_entity_id': bid, 'truth_simulation_time_s': d['sim_time_s']}]
            fields = {'building': _building_words(props)}
            if level == 'collapsed' and rng.random() < CONTRADICTION:
                emit('no_collapse', 'damage', (c.x, c.y), links, fields, props, match='misleading')
                continue
            kind = 'rescue' if serious else {'collapsed': 'collapse', 'severe': 'damage', 'moderate': 'damage', 'light': 'minor'}[level]
            group = None
            if worst is not None and kind in ('rescue', 'damage'):
                gid, group = worst
                links.append({'truth_entity_type': 'people', 'truth_entity_id': gid, 'truth_simulation_time_s': group['sim_time_s']})
            emit(kind, 'rescue' if serious else 'damage', (c.x, c.y), links, fields, props, group=group)

    # --- roads: one event per physical street section (both directions share it) ------------------
    sections = {}
    for rid, s in truth.road_status.items():
        r = m.roads[rid]
        sections.setdefault((min(r['from_node'], r['to_node']), max(r['from_node'], r['to_node'])), []).append((rid, s))
    for ids in sections.values():
        rid, s = ids[0]
        mid = m.road_geoms[rid].interpolate(0.5, normalized=True)
        links = [{'truth_entity_type': 'road', 'truth_entity_id': i, 'truth_simulation_time_s': x['sim_time_s']} for i, x in ids]
        fields = {'street': (m.roads[rid].get('name_en'), m.roads[rid].get('name_ja'))}
        for _ in range(how_many(s['status'])):
            if rng.random() < CONTRADICTION:
                emit('road_open', 'road', (mid.x, mid.y), links, fields, match='misleading', allow_address=False)
            else:
                emit('road_blocked' if s['status'] == 'BLOCKED' else 'road_limited', 'road', (mid.x, mid.y), links, fields,
                     allow_address=False)

    # --- rumours and chatter: nothing true behind them ---------------------------------------------
    real = len(out)
    intact = [b for b, d in truth.building_damage.items() if d['damage_level'] == 'none']
    open_roads = [r for r in m.roads if r not in truth.road_status]
    for _ in range(round(real * RUMOR_SHARE)):
        kind = rng.choice(['rumor_collapse', 'rumor_collapse', 'rumor_fire', 'rumor_road'])
        if kind == 'rumor_road':
            rid = rng.choice(open_roads)
            mid = m.road_geoms[rid].interpolate(0.5, normalized=True)
            emit(kind, 'rumor', (mid.x, mid.y), [], {'street': (m.roads[rid].get('name_en'), m.roads[rid].get('name_ja'))},
                 match='fabricated', allow_address=False)
        else:
            c = m.building_geoms[rng.choice(intact)].centroid
            emit(kind, 'rumor', (c.x, c.y), [], {}, match='fabricated')
    for _ in range(round(real * AMBIENT_SHARE)):
        c = m.building_geoms[rng.choice(intact)].centroid
        emit('ambient', 'ambient', (c.x, c.y), [], {}, match='unmatched')

    return sorted(out, key=lambda x: x['observation']['sim_time_s'])
