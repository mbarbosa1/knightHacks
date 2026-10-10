# Where the data came from, and what was removed from this repo

Written 10 October 2026. The repo was slimmed down before the first push. This file records what
was taken out and what it did, so the knowledge is not lost with the files.

Everything removed is still in the original data package, `sim-data-package.zip` (2,680,260 bytes,
SHA-256 `094e4e8b25929bc7f97ff2739b2a91512b6d8035d0e40e18b6fdbc3e087bd00d`). That zip is not in the
repo. Gerard has a copy; keep one somewhere the team can reach.

## What was removed

| Removed | What it was | Why it went |
|---|---|---|
| `scripts/plateau/` (8 scripts) | Built the buildings and facilities files from the raw sources | Cannot run without the raw source files, which were never in the repo |
| `scripts/load_static.py` | Loaded the three data files into Neon | The database is loaded by hand |
| `scripts/validate.py` | Checked the data files against the schema | Depended on the loader |
| `demo/` (not deleted, gitignored) | 3D MapLibre prototype with its own simplified damage formula | Reference only; not used by the simulator |

Still in the repo: `scripts/build_road_graph.py` and `scripts/load_jshis_soil.py`, which made the
roads and the soil values and would be needed again if the area changes.

## Sources

| Data | Source |
|---|---|
| Buildings | PLATEAU (MLIT, CC BY 4.0): the 2023 release for Shinjuku (city code 13104) and Shibuya (13113), plus the 2020 Tokyo 23-ward CityGML for missing heights and for the demo map's ids |
| Facilities | Overture Maps places, release 2026-09-23.1 |
| Roads | OpenStreetMap, downloaded 10 October 2026 with OSMnx 2.1.1 |
| Soil | J-SHIS surface-ground amplification, 250 m mesh, version V4, area 5339 (file dated 30 May 2022) |

The area is a square reaching 750 m in each direction from Shinjuku Station (35.6938 N, 139.7034 E).
Any building touching the square is kept whole. Footprints under 4 m² were dropped.

## How the buildings were made

The eight `scripts/plateau/` scripts ran in roughly this order.

| Script | What it did |
|---|---|
| `extract.py` | Read the 2020 PLATEAU CityGML and cut it to the box |
| `build.py` | Built the main buildings file from the 2023 data, using the 2020 data where 2023 had gaps. All the inference below happened here |
| `english.py` | Translated Japanese names, addresses and zoning labels to English; originals kept in `*_ja` fields |
| `places.py` | Downloaded the Overture places in the area |
| `facilities.py` | Picked the 13 facilities and matched each to a building |
| `tag_facilities.py` | Wrote `facility_id`, `facility_type` and so on onto the buildings that house a facility |
| `to_db.py` | Reshaped the buildings to one property per database column |
| `demo_join.py` | Matched buildings to the reference demo's ids (only the demo used this) |

### What PLATEAU gave us and what we inferred

Tokyo's PLATEAU data has no structure type and no construction year. Those two are inferred, and
they are the two that matter most to the damage model. Each building's `structure_basis` says which
rule below applied to it.

**Height.** PLATEAU's measured height where it exists; otherwise the 2020 height; otherwise floors ×
3.2 m; otherwise 6 m. `height_source` says which.

**Floors.** PLATEAU's value where it exists; otherwise height ÷ 3.2 m, at least 1.

**Structure.** From PLATEAU's fire-resistance class, the only structural field Tokyo publishes:

| Fire class and size | Structure | Buildings |
|---|---|---|
| Not fire-rated | wood | 873 |
| Semi-fire-resistant, 3 floors or fewer and under 200 m² | wood (modern) | 678 |
| Semi-fire-resistant, larger | light steel | 44 |
| Fire-resistant, 60 m or taller, or 20 floors or more | steel | 35 |
| Fire-resistant, 4 floors or more, or 12 m or taller | reinforced concrete | 2,681 |
| Fire-resistant, low-rise | steel if 300 m² or more, otherwise concrete | 480 |
| No fire class, map class "solid building" | steel if 60 m or taller, otherwise concrete | 45 |
| No attributes, 10 m or lower and under 150 m² | wood | 422 |
| No attributes, anything else | steel if 60 m or taller, otherwise concrete | 77 |

Only the first row (873 buildings) counts as real data in `structure_source`. The other 4,462 are
marked `inferred`.

**Age.** No building has a real construction year. Each got a set of odds for three age bands,
chosen by building type. These odds are rough assumptions for central Tokyo, not measurements:

| Building type | Before 1971 | 1971–1981 | After 1981 | Buildings |
|---|---|---|---|---|
| Wood (not fire-rated, or no attributes) | 25% | 25% | 50% | 1,295 |
| Modern wood (semi-fire-resistant) | 0% | 5% | 95% | 678 |
| 60 m or taller | 0% | 35% | 65% | 33 |
| Everything else | 10% | 20% | 70% | 3,329 |

The odds are stored on each building as `era_probs`, and the simulator re-rolls the age on every
run. The stored `era` is a single draw with a fixed seed (13104). The stored `year_built` is a
second random draw inside that band (1950–1970, 1971–1981 or 1982–2020), which is why nothing should
rely on it.

**Use.** PLATEAU's usage code where it exists (about 90% of buildings). Otherwise: wood under 200 m²
is a house, larger wood is an apartment, non-wood 45 m or taller is an office, and the rest is mixed
commercial.

**Ids.** `id` is PLATEAU's 2023 identifier and is unique. `plateau_building_id` is PLATEAU's own
building number. `map_id` is the 2020 id used by the demo map: 664 buildings have none, and it is
not unique among the rest (4,593 distinct values), so it cannot be used as a key.

### How the facilities were chosen

The 13 facilities were picked by hand from the Overture places and given English names. Each was
matched to the building containing its point, or failing that to the nearest building within about
25 m (`building_match` says which).

Two caveats recorded at the time:

- The three schools are assumed to be shelters, on the basis that Shinjuku uses its public schools
  as evacuation shelters. They were not checked against the official list.
- Shinjuku Gyoen is included as a large open-space evacuation area, also not checked against the
  official list.

`capacity` is empty for all 13 because it is not known.

## What the validator checked

`scripts/validate.py` was run on 10 October 2026 against the files now in `data/` and reported 0
errors. It checked, for buildings, facilities and roads:

- geometry type, and that coordinates are longitude/latitude in Japan;
- every expected column present, with the right type and an allowed value;
- no duplicate ids, and required fields not empty;
- each building's age odds add up to 1;
- every facility's `building_id` exists in the buildings, and every building's `facility_id` exists
  in the facilities;
- every facility's `access_node` is a node in the roads;
- every road id has the form `<from_node>_<to_node>_<key>`.

Its one warning was that the roads' `highway` property has no database column.

The data files have not changed since that run. If they are regenerated, nothing in the repo checks
them any more.

## What the loader did

`scripts/load_static.py` inserted one row into `static.map_versions`, then the buildings,
facilities and roads, all in one transaction under map version `shinjuku-v1`. It converted each
GeoJSON geometry with `ST_SetSRID(ST_GeomFromGeoJSON(...), 4326)`, stored `era_probs` as `jsonb`, and
left out the roads' `highway` property. The package's author tested it and `sql/schema.sql` on a
local PostGIS 16 with the earlier data; it was never run against Neon from this repo.
