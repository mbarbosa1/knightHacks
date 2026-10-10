# Shinjuku disaster-response simulator

Knight Hacks project. A simulator creates an earthquake around Shinjuku Station, Tokyo, and decides what
really happened; AI agents work from noisy reports to respond; "reveal truth" scores them.

This branch holds the simulation. See [DEVLOG.md](DEVLOG.md) for what it does and how it was checked.

## Run it

```bash
python3 -m venv .venv
.venv/bin/pip install -r sim-data-package/requirements.txt
cd sim-data-package
../.venv/bin/python -m sim.run --seed 42            # random earthquake from the seed
../.venv/bin/python -m sim.run --seed 42 --mw 7.3   # same seed, fixed magnitude
../.venv/bin/python -m pytest tests
```

## Layout

| Path | What |
|---|---|
| `sim-data-package/sim/` | Simulator: scenario, shaking and damage, debris on roads, truth state, storage |
| `sim-data-package/data/` | Buildings (PLATEAU), roads (OpenStreetMap), facilities, soil (J-SHIS) |
| `sim-data-package/scripts/` | How the roads and soil values were made: road graph download, soil loader |
| `sim-data-package/docs/` | Database design, damage model reference, data coverage, and [PROVENANCE.md](sim-data-package/docs/PROVENANCE.md): how the data was made and what was removed from the repo |
| `sim-data-package/sql/` | Neon schema |
