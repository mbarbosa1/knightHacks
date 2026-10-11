"""Agent tests. From the repo root:

    .venv/bin/python -m pytest agents/tests                 # fast: no model, no database, no network
    AGENTS_LIVE_RUN=<run_id> .venv/bin/python -m pytest agents/tests -m live   # real model + Neon (tunnel open)
"""
import pathlib, sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))   # repo root, so `agents.*` imports


def pytest_configure(config):
    config.addinivalue_line('markers', 'live: needs the SSH tunnel to the model, ./dev.sh and Neon (AGENTS_LIVE_RUN)')
