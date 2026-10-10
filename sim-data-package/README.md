# Shinjuku simulation data package

Everything the simulator and database need for the Shinjuku demo box (1.5 km around Shinjuku Station). Copy this folder into the repo as-is.

## Setup

> Note (10 Oct): `scripts/load_static.py` and `scripts/validate.py` are no longer in the repo; the database is loaded by hand. The commands below that use them are kept for reference only.

```bash
pip install -r requirements.txt
psql "$OWNER_URL" -f sql/schema.sql                       # once, as the Neon owner (creates static + ops schemas, roles)
python3 scripts/validate.py                                 # checks the data files against the schema
DATABASE_URL="$MAP_IMPORT_URL" python3 scripts/load_static.py   # loads map version "shinjuku-v1"
```

After the road graph or J-SHIS data is added, reload with `load_static.py --replace` (it refuses if a simulation run already points at that map version; use a new `--map-version` instead).

## Files

| Path | What it is | Used by |
|---|---|---|
| `data/buildings.geojson` | 5,335 building polygons, one property per `static.buildings` column | loader, simulator, map |
| `data/facilities.geojson` | 13 hand-checked facilities (2 hospitals, 2 fire stations, 5 police boxes, 4 shelters) | loader, resource/routing |
| `data/bbox.json` | Building extent, and the road download box (extent + 150 m) | road graph script |
| `data/plateau_full/` | Same buildings with a `source` per field and demo ids, plus extraction stats | reference only |
| `sql/schema.sql` | Neon schema: `static` + `ops` tables, PostGIS, GiST indexes, roles `map_import` / `simulator` / `agents` | DB setup |
| `scripts/load_static.py` | Loads buildings, facilities and roads (if present) into `static` in one transaction | data pipeline |
| `scripts/validate.py` | Offline check of types, enums, geometry, ids and cross references | before every load |
| `scripts/build_road_graph.py` | OSMnx download: writes GraphML, `road_segments.geojson` (`u_v_key` ids), fills facility `access_node` | run on a laptop |
| `scripts/load_jshis_soil.py` | Puts J-SHIS soil amplification (ARV) into `buildings.soil_amp` by 250 m mesh | after J-SHIS download |
| `sim/damage_model.py` | Shaking → intensity → damage formulas (pure Python) | simulator |
| `CLAUDE.md` | Handoff briefing for the simulation branch (Claude Code reads it automatically) | Gerard / Claude Code |
| `docs/` | Agreed DB design, simulator runtime, damage model reference, data coverage, project context | everyone |
| `demo/` | Working MapLibre prototype and its sources | frontend reference |

## Still missing

1. **Road graph.** The sandbox can't reach OpenStreetMap. On a laptop: `python3 scripts/build_road_graph.py`, then `validate.py` and `load_static.py --replace`. It writes `data/road_graph.graphml`, `data/road_segments.geojson` and `data/road_segments.meta.json`, and fills `access_node` in `data/facilities.geojson`. Load the graph in the simulator with `osmnx.load_graphml`; a road's id is `f"{u}_{v}_{key}"`, the same as `static.road_segments.id`. Both directions of a two-way street are separate rows. Tested here only on a synthetic graph.
2. **J-SHIS soil.** Download the surface-ground 250 m mesh for area 5339 on a laptop (blocked here), put the CSV in `data/jshis/`, run `scripts/load_jshis_soil.py data/jshis/<file>.csv`, then reload. Until then `soil_amp` is NULL; the damage model then uses the Vs=400 baseline. Tested on a fake CSV with the expected `CODE`/`ARV` columns; check the printed column names on the real file.
3. **Facility `access_node`.** Filled by step 1.
4. **Facility `capacity`.** NULL for all 13 (unknown).

## Things to know

- `year_built` is filled for every building but is a seeded guess inside the drawn era (`year_source = inferred`). The DB rule is "unknown stays NULL"; ignore it in the simulator and use `era` / `era_probs`.
- `damage_model.sample_damage` returns `collapse / half / none`; the truth tables use `none / light / moderate / severe / collapsed`. The simulator needs a mapping.
- References from `ops` into `static` (incident `building_id`, restriction `road_id`, resource `home_facility_id`) are checked by trigger against the run's map version, since the version is stored only on `simulation_runs`.
- Roles are created without passwords: `ALTER ROLE agents WITH PASSWORD '...'` (same for `map_import`, `simulator`).
- Data sources: PLATEAU 2023/2020 (MLIT, CC BY 4.0), Overture Maps places 2026-09-23.1; see `docs/COVERAGE.md`.
