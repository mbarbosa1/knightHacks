'''
Drone recon: an aerial look at one place, to help the operator decide.

Why: the agents work from reports, and three things in a disaster cannot be settled from reports alone:
  1. Where exactly is it?  A report "near the station" fits dozens of buildings.
  2. Is it real?           Many incidents rest on one report, some of them rumours.
  3. Can we get there?     Routes use the roads believed open; units find blocked roads only on arrival.
A drone pass answers all three. This module makes the images and has the vision model read them:

  before  the real aerial photo of the area: GSI seamless photo tiles (Geospatial Information Authority of Japan,
          https://maps.gsi.go.jp/development/ichiran.html, about 0.5 m per pixel), stitched around the place.
  after   a SIMULATED post-earthquake drone image: the same photo with the simulation's ground truth drawn in
          (rubble on collapsed footprints, debris spilling into the street, blocked roads). Always labelled
          SIMULATED. It shows what a drone would see, so the vision model's reading is a sensor, not the truth.
  reading Qwen3-VL (agents/llm.py) reads the after image with the candidate buildings lettered on it: damage seen,
          which building, whether the approach streets are passable, where to stage, how sure it is.

The reading can be sent to the agents as a 'sensor' report (POST /recon/{id}/report in sim/server.py), so the
existing pipeline folds it into the incident like any other report. Nothing else changes.
'''
import base64, io, json, math, pathlib, random, sys, time, uuid
import httpx
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

ROOT = pathlib.Path(__file__).resolve().parents[2]
CACHE = ROOT / 'simulation' / 'cache' / 'gsi'            # gitignored (simulation/cache/)
OUT = ROOT / 'simulation' / 'runs'                       # recon images live with their run (gitignored)
TILE_URL = 'https://cyberjapandata.gsi.go.jp/xyz/seamlessphoto/{z}/{x}/{y}.jpg'
ZOOM, TILE = 18, 256                                     # zoom 18: ~0.48 m per pixel over Tokyo
VIEW = 360                                               # area fetched, photo pixels (~175 m across)
SCALE = 2                                                # shown and read at 2x (720 px), so a house is visible
LETTERS = 'ABCDE'
DAMAGE_ORDER = ('none', 'light', 'moderate', 'severe', 'collapsed')


# =============================================================================
# 1. THE REAL AERIAL PHOTO (GSI tiles, cached on disk)
# =============================================================================

def _world_px(lon, lat):
    n = TILE * 2 ** ZOOM
    return ((lon + 180) / 360 * n,
            (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)


def _tile(x, y):
    path = CACHE / f'{ZOOM}_{x}_{y}.jpg'
    if not path.exists():
        r = httpx.get(TILE_URL.format(z=ZOOM, x=x, y=y), timeout=20)
        r.raise_for_status()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(r.content)
    return Image.open(path).convert('RGB')


def aerial(lon, lat, size=VIEW):
    """(image, project): the photo centred on lon/lat, and a function lon/lat -> pixel in it."""
    cx, cy = _world_px(lon, lat)
    left, top = cx - size / 2, cy - size / 2
    img = Image.new('RGB', (size, size), (200, 200, 200))
    for tx in range(int(left // TILE), int((left + size) // TILE) + 1):
        for ty in range(int(top // TILE), int((top + size) // TILE) + 1):
            img.paste(_tile(tx, ty), (int(tx * TILE - left), int(ty * TILE - top)))

    def project(lo, la):
        px, py = _world_px(lo, la)
        return px - left, py - top
    return img, project


# =============================================================================
# 2. THE SIMULATED DRONE IMAGE (ground truth drawn onto the real photo)
# =============================================================================

def _polygon_px(geom, project):
    """Exterior ring(s) of a footprint, in pixels."""
    parts = getattr(geom, 'geoms', [geom])
    return [[project(x, y) for x, y in part.exterior.coords] for part in parts]


def _rubble_texture(before, seed):
    """Collapsed concrete seen from above: the photo's own structure, greyed and broken into slabs (each with a
    lit side and a shadow), with dark gaps between them. Built for the whole image, then masked per building."""
    w, h = before.size
    g = random.Random(seed)
    base = ImageEnhance.Brightness(ImageEnhance.Color(before).enhance(0.15)).enhance(0.8).convert('RGBA')
    slabs = Image.new('RGBA', (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(slabs)
    for _ in range(w * h // 6):
        x, y = g.uniform(0, w), g.uniform(0, h)
        size, angle = g.uniform(0.8, 3.2), g.uniform(0, math.pi)
        tone = g.randint(120, 190)
        tint = (tone, tone - g.randint(0, 6), tone - g.randint(4, 14), 255) if g.random() > 0.08 else \
               (tone - 15, tone - 30, tone - 40, 255)                      # the odd rust / brick fragment
        pts = [(x + size * math.cos(angle + a) * g.uniform(0.6, 1.1), y + size * math.sin(angle + a) * g.uniform(0.4, 0.9))
               for a in (0, math.pi / 2, math.pi, 3 * math.pi / 2)]
        d.polygon([(px + 0.8, py + 0.8) for px, py in pts], fill=(45, 42, 38, 150))   # its shadow
        d.polygon(pts, fill=tint)
    out = Image.alpha_composite(base, slabs)
    return ImageEnhance.Contrast(out.convert('RGB')).enhance(1.08).filter(ImageFilter.GaussianBlur(0.35)).convert('RGBA')


def _jagged(mask_draw, poly, rng, spill_px):
    """The footprint plus debris spilling past its edges in irregular lobes."""
    mask_draw.polygon(poly, fill=255)
    if spill_px <= 0:
        return
    for (x0, y0), (x1, y1) in zip(poly, poly[1:] + poly[:1]):
        for _ in range(max(1, int(math.hypot(x1 - x0, y1 - y0) / 6))):
            t = rng.random()
            x, y = x0 + (x1 - x0) * t, y0 + (y1 - y0) * t
            r = rng.uniform(0.3, 1.0) * spill_px
            mask_draw.ellipse([x - r, y - r * rng.uniform(0.5, 1), x + r, y + r * rng.uniform(0.5, 1)], fill=255)


def simulated_after(before, project, static_map, damage, roads, seed, stamp):
    """The photo as a drone would see it after the earthquake, from the simulation's truth for this area:
    collapsed = rubble field with debris spilling into the street, severe = partly collapsed, moderate = cracks,
    blocked / limited road = debris across it. Everything else is left exactly as the real photo."""
    rng = random.Random(seed)
    img = before.convert('RGBA')
    w, h = img.size
    texture = _rubble_texture(before.convert('RGB'), seed)
    heavy = Image.new('L', (w, h), 0)                  # where rubble covers the ground
    heavy_draw = ImageDraw.Draw(heavy)
    partial = Image.new('L', (w, h), 0)
    partial_draw = ImageDraw.Draw(partial)
    cracks = ImageDraw.Draw(img)
    dust = Image.new('L', (w, h), 0)
    dust_draw = ImageDraw.Draw(dust)
    for bid, geom in static_map.building_geoms.items():
        level = (damage.get(bid) or {}).get('damage_level')
        if level not in ('moderate', 'severe', 'collapsed'):
            continue
        floors = (static_map.buildings.get(bid) or {}).get('floors') or 3
        for poly in _polygon_px(geom, project):
            if not any(-30 < x < w + 30 and -30 < y < h + 30 for x, y in poly):
                continue
            if level == 'collapsed':                    # debris reach grows with height (~half the height, in px)
                _jagged(heavy_draw, poly, rng, spill_px=min(12, 2 + floors * 0.9))
                dust_draw.polygon(poly, fill=140)
            elif level == 'severe':                     # one part of the building came down
                xs, ys = [p[0] for p in poly], [p[1] for p in poly]
                cx, cy = rng.uniform(min(xs), max(xs)), rng.uniform(min(ys), max(ys))
                r = 0.35 * max(max(xs) - min(xs), max(ys) - min(ys))
                blob = Image.new('L', (w, h), 0)
                ImageDraw.Draw(blob).ellipse([cx - r, cy - r, cx + r, cy + r], fill=255)
                foot = Image.new('L', (w, h), 0)
                _jagged(ImageDraw.Draw(foot), poly, rng, spill_px=min(10, 2 + floors * 0.6))
                partial.paste(255, mask=Image.fromarray(
                    (__import__('numpy').minimum(__import__('numpy').array(blob), __import__('numpy').array(foot)))))
                dust_draw.polygon(poly, fill=60)
            else:                                       # moderate: a few dark cracks across the roof
                xs, ys = [p[0] for p in poly], [p[1] for p in poly]
                for _ in range(2):
                    x0, y0 = rng.uniform(min(xs), max(xs)), rng.uniform(min(ys), max(ys))
                    pts = [(x0, y0)]
                    for _ in range(4):
                        x0, y0 = x0 + rng.uniform(-6, 6), y0 + rng.uniform(-6, 6)
                        pts.append((x0, y0))
                    cracks.line(pts, fill=(40, 38, 36, 255), width=1)
    for rid, status in roads.items():
        if status not in ('BLOCKED', 'LIMITED'):
            continue
        line = static_map.road_geoms.get(rid)
        if line is None:
            continue
        pts = [project(x, y) for x, y in line.coords]
        if not any(0 <= x <= w and 0 <= y <= h for x, y in pts):
            continue
        mx, my = pts[len(pts) // 2]
        r = 5 if status == 'BLOCKED' else 3
        _jagged(heavy_draw, [(mx - r, my - r), (mx + r, my - r * 0.6), (mx + r * 0.7, my + r), (mx - r, my + r * 0.8)],
                rng, spill_px=r * 0.8)
    # shadow first (the rubble sits low, edges cast short shadows), then the rubble itself
    shadow = heavy.filter(ImageFilter.GaussianBlur(2)).point(lambda v: int(v * 0.2))
    img = Image.composite(Image.new('RGBA', (w, h), (20, 18, 16, 255)), img, shadow)
    img = Image.composite(texture, img, heavy.filter(ImageFilter.GaussianBlur(0.8)))
    img = Image.composite(texture, img, partial.filter(ImageFilter.GaussianBlur(1.2)).point(lambda v: int(v * 0.85)))
    haze = Image.new('RGBA', (w, h), (222, 214, 200, 255))
    img = Image.composite(haze, img, dust.filter(ImageFilter.GaussianBlur(16)).point(lambda v: int(v * 0.45)))
    img = ImageEnhance.Color(img.convert('RGB')).enhance(0.85)              # dusty, overcast air
    img = ImageEnhance.Contrast(img).enhance(0.95)
    return img


# =============================================================================
# 3. LABELS: candidate buildings lettered, so the model and the operator talk about the same thing
# =============================================================================

def _font(size):
    for name in ('/System/Library/Fonts/Supplemental/Arial Bold.ttf', '/System/Library/Fonts/SFNSMono.ttf',
                 '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _caption(img, text):
    draw = ImageDraw.Draw(img)
    font = _font(13)
    x0, y0, x1, y1 = draw.textbbox((0, 0), text, font=font)
    draw.rectangle([8, 8, 8 + (x1 - x0) + 14, 8 + (y1 - y0) + 10], fill=(24, 33, 43))
    draw.text((15, 12), text, font=font, fill=(255, 255, 255))


def outline(img, candidates, project, static_map, centre, neutral=False):
    """Each candidate footprint outlined (the first, the incident's own building when known, in red) and its letter
    in a tag set off to the side with a thin leader line, so tags never cover the damage being looked at."""
    img = img.copy()
    draw = ImageDraw.Draw(img)
    font = _font(15)
    w, h = img.size
    cx, cy = project(*centre)
    placed = []
    for i, c in enumerate(candidates):
        color = (230, 57, 70) if i == 0 and not neutral else (255, 214, 10)
        polys = _polygon_px(static_map.building_geoms[c['building_id']], project)
        for poly in polys:
            draw.line(poly + [poly[0]], fill=color, width=2)
        bx, by = c['px']
        reach = max(math.hypot(x - bx, y - by) for poly in polys for x, y in poly) + 22
        # away from the reported point (or, for the building on it, up and to the left), turned until it is clear
        base = math.atan2(by - cy, bx - cx) if math.hypot(bx - cx, by - cy) > 4 else -2.4
        for turn in (0, 0.7, -0.7, 1.4, -1.4, 2.1, -2.1, 3.14):
            a = base + turn
            tx = min(w - 14, max(14, bx + reach * math.cos(a)))
            ty = min(h - 14, max(40, by + reach * math.sin(a)))
            if all(math.hypot(tx - px, ty - py) > 30 for px, py in placed):
                break
        placed.append((tx, ty))
        draw.line([bx, by, tx, ty], fill=color, width=1)
        draw.rectangle([tx - 11, ty - 11, tx + 11, ty + 11], fill=(24, 33, 43), outline=color, width=2)
        x0, y0, x1, y1 = draw.textbbox((0, 0), c['label'], font=font)
        draw.text((tx - (x1 - x0) / 2 - x0, ty - (y1 - y0) / 2 - y0), c['label'], font=font, fill=(255, 255, 255))
    draw.line([cx - 7, cy, cx + 7, cy], fill=(200, 16, 46), width=2)
    draw.line([cx, cy - 7, cx, cy + 7], fill=(200, 16, 46), width=2)
    return img


def candidates_near(static_map, lon, lat, project, building_id=None, k=4, radius_m=60):
    """The incident's building first (when known), then the nearest buildings: lettered A, B, C..."""
    from shapely.geometry import Point
    from .roads import to_metres
    p = to_metres(Point(lon, lat))
    near = []
    for bid, geom in static_map.building_geoms.items():
        d = to_metres(geom).distance(p)
        if d <= radius_m:
            near.append((d, bid))
    near.sort()
    ids = ([building_id] if building_id in static_map.building_geoms else []) + [b for _, b in near if b != building_id]
    out = []
    for label, bid in zip(LETTERS, ids[:k]):
        props = static_map.buildings.get(bid) or {}
        c = static_map.building_geoms[bid].centroid
        out.append({'label': label, 'building_id': bid, 'px': project(c.x, c.y),
                    'name': props.get('name_en') or (props.get('address_en') or '').split(',')[0] or None,
                    'usage': props.get('usage'), 'floors': props.get('floors')})
    return out


# =============================================================================
# 4. THE VISION MODEL'S READING
# =============================================================================

READ_PROMPT = '''You compare two top-down aerial images of the same city block in Tokyo, north up, about 175 m across.
LEFT = before an earthquake (real photo). RIGHT = drone image after it. Buildings A-D are outlined in yellow in both,
each with a letter tag on a thin line. For EACH letter, compare that outline LEFT vs RIGHT:
 none      = the roof looks the same in both
 moderate  = the same roof with a few new dark cracks or small fragments
 severe    = part of the roof is replaced by grey rubble
 collapsed = the roof is gone: the whole outline is a grey-brown field of broken pieces, often spilling past it
Then look at the streets around the buildings in the RIGHT image for new rubble. Only report differences you can see.
Reply with ONLY a JSON object with these keys:
 "buildings": object mapping each letter to none, moderate, severe, collapsed or unclear
 "debris_on_streets": list of {"where", "passable"} for streets with NEW rubble on them only; "where" names the
             street by its direction from building A and kind (wide road, narrow lane); "passable" is yes, one lane
             or no. Empty list when no street has new rubble.
 "approach": one sentence: the side to approach building A from, using streets without rubble
 "staging": one sentence: a visible open area (car park, plaza, wide road) near A where units could stop, or null
 "confidence": 0 to 1
 "summary": two sentences for the operator: what changed and what it means for the response'''


def _data_uri(img):
    buf = io.BytesIO()
    img.save(buf, 'JPEG', quality=85)
    return 'data:image/jpeg;base64,' + base64.b64encode(buf.getvalue()).decode()


def read_image(img, context):
    """Qwen3-VL's reading of the labelled after image. Uses agents/llm.py (hosted when configured, else AMD)."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from agents.llm import ask_json
    # Blind on purpose: told what the reports claim, the model "saw" a collapse on an intact block. The drone is
    # only worth having as a check that does not depend on the reports. `context` is kept for the record.
    raw = ask_json(READ_PROMPT, 'Compare LEFT (before) and RIGHT (after).', image_url=_data_uri(img), max_tokens=700,
                   timeout=180)
    per = {k: v for k, v in (raw.get('buildings') or {}).items() if k in LETTERS and v in DAMAGE_ORDER + ('unclear',)} \
        if isinstance(raw.get('buildings'), dict) else {}
    # The incident's building is A; if A looks intact but another marked building has collapsed, that is the one.
    rank = lambda v: DAMAGE_ORDER.index(v) if v in DAMAGE_ORDER else -1
    worst = max(per, key=lambda k: rank(per[k]), default=None)
    pick = 'A' if rank(per.get('A')) >= 3 or worst is None or per[worst] != 'collapsed' else worst
    seen = per.get(pick) if per.get(pick) in DAMAGE_ORDER else None
    raw['building'] = pick if seen and seen != 'none' else None
    raw['collapse_visible'] = any(v == 'collapsed' for v in per.values())
    conf = raw.get('confidence')
    return {
        'damage_seen': seen,
        'buildings': per,
        'building': raw.get('building') if raw.get('building') in tuple(LETTERS) else None,
        'collapse_visible': bool(raw.get('collapse_visible')),
        'debris_on_streets': [d for d in raw.get('debris_on_streets') or [] if isinstance(d, dict)][:6],
        'approach': raw.get('approach') or None,
        'staging': raw.get('staging') or None,
        'confidence': round(min(1.0, max(0.0, float(conf))), 2) if isinstance(conf, (int, float)) else None,
        'summary': str(raw.get('summary') or '').strip() or None,
    }


# =============================================================================
# 5. ONE PASS
# =============================================================================

def fly(run_id, static_map, truth_damage, truth_roads, lon, lat, building_id=None, context='', sim_time_s=0.0):
    """Make the before / after images for one place and have the vision model read the after image."""
    recon_id = str(uuid.uuid4())
    folder = OUT / str(run_id) / 'recon'
    folder.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    before, project = aerial(lon, lat)
    stamp = f'T+{int(sim_time_s // 60):02d}:{int(sim_time_s % 60):02d}'
    after = simulated_after(before, project, static_map, truth_damage, truth_roads, seed=f'{run_id}:{lon:.5f}:{lat:.5f}',
                            stamp=stamp)
    # enlarge, then draw the labels at full size so they stay sharp
    size = (before.width * SCALE, before.height * SCALE)
    before, after = before.resize(size, Image.LANCZOS), after.resize(size, Image.LANCZOS)
    big = lambda lo, la: tuple(v * SCALE for v in project(lo, la))
    cands = candidates_near(static_map, lon, lat, big, building_id)
    before_l = outline(before, cands, big, static_map, (lon, lat))
    _caption(before_l, 'GSI AERIAL PHOTO · BEFORE THE EARTHQUAKE')
    after_l = outline(after, cands, big, static_map, (lon, lat))
    _caption(after_l, f'SIMULATED DRONE IMAGE · {stamp}')
    before_l.save(folder / f'{recon_id}-before.jpg', quality=88)
    after_l.save(folder / f'{recon_id}-after.jpg', quality=88)
    # what the model sees: before | after, every outline the same colour (a red one drew its eye to "damage")
    pair = Image.new('RGB', (before.width * 2 + 12, before.height), (255, 255, 255))
    pair.paste(outline(before, cands, big, static_map, (lon, lat), neutral=True), (0, 0))
    pair.paste(outline(after, cands, big, static_map, (lon, lat), neutral=True), (before.width + 12, 0))
    reading = read_image(pair.resize((pair.width * 3 // 4, pair.height * 3 // 4), Image.LANCZOS), context)
    by_label = {c['label']: c for c in cands}
    result = {
        'id': recon_id, 'run_id': str(run_id), 'longitude': lon, 'latitude': lat, 'sim_time_s': sim_time_s,
        'before': f'/recon/images/{run_id}/{recon_id}-before.jpg', 'after': f'/recon/images/{run_id}/{recon_id}-after.jpg',
        'candidates': [{k: v for k, v in c.items() if k != 'px'} for c in cands],
        'reading': reading,
        'matched_building_id': (by_label.get(reading['building']) or {}).get('building_id'),
        'seconds': round(time.monotonic() - started, 1),
    }
    (folder / f'{recon_id}.json').write_text(json.dumps(result, ensure_ascii=False, indent=1))
    return result


def as_report(result):
    """The reading as an ops.observations 'sensor' report, for the agents (POST /runs/{id}/observations)."""
    r = result['reading']
    roads = '; '.join(f"{d.get('where')}: {d.get('passable')}" for d in r['debris_on_streets']) or 'no debris seen on streets'
    match = next((c for c in result['candidates'] if c['building_id'] == result['matched_building_id']), None)
    where = f"building {match['label']}" + (f" ({match['name']})" if match and match.get('name') else '') if match else 'the reported spot'
    text = (f"DRONE RECON: damage {r['damage_seen'] or 'unclear'} at {where}"
            f"{', collapse visible' if r['collapse_visible'] else ''}. Streets: {roads}. "
            f"Approach: {r['approach'] or 'n/a'}. Staging: {r['staging'] or 'n/a'}. {r['summary'] or ''}").strip()
    return {'id': str(uuid.uuid5(uuid.UUID(result['id']), 'report')), 'channel': 'sensor', 'format': 'structured',
            'original_text': text, 'lang': 'en', 'text_en': text, 'media_url': None,
            'claimed_location': where, 'location_precision': 'gps',
            'location': {'longitude': result['longitude'], 'latitude': result['latitude']},
            'source_reliability': round(0.6 + 0.35 * (r['confidence'] or 0.5), 2), 'sim_time_s': result['sim_time_s']}
