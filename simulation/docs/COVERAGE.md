# Shinjuku buildings: what is real and what is inferred

Area: the demo's 1.5 km × 1.5 km box around Shinjuku Station (centre 35.6938 N, 139.7034 E), same clip as `demo/source/prep.py`.

## Sources

| Source | What it gave us | How it was fetched |
|---|---|---|
| **PLATEAU 2023, Shinjuku-ku and Shibuya-ku** (MLIT; repackaged as "Flateau" GeoParquet by Pacific Spatial Solutions, CC BY 4.0) | footprints, height, floors, usage, fire-resistance class, GSI building class, zoning. Survey year 2021. | `s3://us-west-2.opendata.source.coop/pacificspatial/flateau/parquet/131{04,13}_*_2023_building_{lod0,detail}.parquet` |
| **PLATEAU 2020 CityGML, Tokyo 23 wards** (official v3 zip) | the building IDs the demo uses (`13104-bldg-NNNN`), plus a height fallback | six 1 km mesh files (5339452[56], 5339453[56], 5339454[56]) range-read out of the 4.5 GB zip on the G-Spatial S3 mirror `gsic-opendata` |

The official download hosts (geospatial.jp, assets.cms.plateau.reearth.io, api.plateau.reearth.io) are blocked from the build sandbox, so both sources above are mirrors of the same official data.

## Coverage (5,335 buildings in the box)

| Field | Real (from PLATEAU) | Notes |
|---|---|---|
| Footprint | 5,335 (100%) | PLATEAU 2023 LOD0 |
| Height | 5,140 (96%) | rest from floors × 3.2 m or 6 m default |
| Floors | 4,809 (90%) | rest = height / 3.2 m |
| Usage | 4,783 (90%) | house 781, house+shop 508, apartment 902, office 967, commercial 1,137, hotel 110, ... |
| Fire-resistance class | 4,791 (90%) | fire-resistant 3,196 · semi-fire-resistant 722 · not fire-rated (wood) 873 |
| **Structure (wood / RC / steel)** | **wood vs non-wood: 90% real; RC vs steel: 0% real** | see below |
| **Year built** | **0 (0%)** | Tokyo publishes no construction year in any PLATEAU release (checked 2020 and 2023). |

**Structure.** Tokyo's PLATEAU data has no structure-type field (`buildingStructureType` is empty everywhere). The fire-resistance class is the closest real signal and is used like this (`structure_basis` on each building says which rule fired):

- Not fire-rated → **wood**: 873 buildings, counted as real.
- Semi-fire-resistant: ≤ 3 floors and < 200 m² → **wood** (modern fire-rated timber, typical in Tokyo's fire-prevention districts); otherwise **light_steel**. 722 buildings.
- Fire-resistant: ≥ 60 m or ≥ 20 floors → **steel** (S/SRC); ≥ 4 floors or ≥ 12 m → **rc**; else rc or steel by footprint. 3,196 buildings.
- No attributes (544 buildings, mostly ones built after the 2021 survey): national map class "solid building" → non-wood, else small low-rise → wood.

Totals: wood 1,973 · rc 3,247 · steel 71 · light_steel 44.

**Era (pre-1971 / 1971–81 / post-1981).** Every era is inferred. Each building carries `era_probs` (a prior by type) and one seeded draw in `era`. The priors are rough assumptions, not measured values:

| Type | pre-1971 | 1971–81 | post-1981 |
|---|---|---|---|
| wood (not fire-rated) | 0.25 | 0.25 | 0.50 |
| wood (quasi-fireproof) | 0.00 | 0.05 | 0.95 |
| non-wood | 0.10 | 0.20 | 0.70 |
| tall (≥ 60 m) | 0.00 | 0.35 | 0.65 |

In the simulator, resample the era on every Monte Carlo run (`damage_model.sample_damage` does this) so the uncertainty shows up in the results. If you calibrate against Tokyo's official estimate, these priors are the knob to turn.

## Matching to the demo

PLATEAU 2023 renumbered every building ID, so the match is spatial.

- `demo_building_attributes.json` is keyed by the demo's feature `id` (0–4513). **4,289 of 4,514** demo buildings matched a 2023 building covering ≥ 50% of their footprint. The other 225 (mostly demolished or rebuilt since 2020, or slivers at the clip edge) get size-based inferred attributes and `plateau_id: null`.
- In `shinjuku_buildings.geojson` each building also has `id_2020` (the demo's `bid` scheme), `demo_id` and `demo_iou` where a 1-to-1 match exists.

## Language

Every value in both data files is in English. Where PLATEAU had Japanese text (building names, addresses, map class, zoning), the original is kept in a matching `*_ja` field, and PLATEAU's numeric codes are kept in `usage_code` and `fireproof_code`.

## Files

- `shinjuku_buildings.geojson`: 5,335 polygons, WGS84, with every field above and a `source` object per field (`PLATEAU`, `PLATEAU_2020`, `PLATEAU+inferred` or `inferred`).
- `demo_building_attributes.json`: the same attributes keyed by demo feature id, ready to drop into the demo.
- `REFERENCE.md` and `damage_model.py`: the shaking → intensity → damage formulas.
- `scripts/`: the extraction scripts. They expect the downloads in a local folder; the URLs are above.
