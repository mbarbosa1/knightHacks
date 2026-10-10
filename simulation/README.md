# Simulation

Creates an earthquake around Shinjuku Station and decides what really happened: damage to each of
5,335 buildings, blocked roads, people hurt or trapped, and the noisy reports that agents see. The
same seed always gives the same disaster.

## Run it

```bash
python3 -m venv .venv                                  # from the repo root
.venv/bin/pip install -r simulation/requirements.txt
cd simulation
../.venv/bin/python -m sim.run --seed 42               # random earthquake from the seed
../.venv/bin/python -m sim.run --seed 42 --mw 7.3      # same seed, fixed magnitude
../.venv/bin/python -m pytest tests
```

**The map data is not in this repo.** The simulator reads it from `simulation/data/`, which is
gitignored. Until the simulator can load the map from the backend API, that folder has to be copied
from someone who has it.

A run writes its results to `simulation/runs/<run id>/` (gitignored).

## What is here

| Path | What |
|---|---|
| `sim/scenario.py` | A seed becomes an earthquake: magnitude, depth, epicentre |
| `sim/damage_model.py`, `sim/damage.py` | Shaking at each building and one of five damage levels |
| `sim/roads.py` | Roads blocked or limited by debris |
| `sim/people.py` | Who is in each building and their condition |
| `sim/reports.py` | The reports agents see, and the hidden answer key |
| `sim/routing.py` | Routes, fallbacks, nearest units, unit-to-incident assignment |
| `sim/lookup.py` | A stated place to a road id or building id |
| `sim/truth.py`, `sim/storage.py` | The hidden truth in memory, and reading the map / saving a run |
| `sim/run.py` | Runs all of the above |
| `tests/` | 39 checks |
| `scripts/` | How the roads and soil values were made |
| `docs/` | Damage formulas and sources, data coverage, where the data came from |
| `CLAUDE.md` | Working notes and decisions for this part of the project |

## Not built yet

Loading the map from the backend API, the live loop (units moving, reports released over time),
sending the hidden truth to Databricks, and flood.
