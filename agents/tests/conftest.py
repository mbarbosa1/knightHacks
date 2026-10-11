"""Agent tests. From the repo root:

    .venv/bin/python -m pytest agents/tests                 # fast: no model, no database, no network
    AGENT_LLM=on AGENTS_LIVE_RUN=<run_id> .venv/bin/python -m pytest agents/tests -m live   # real model + Neon (tunnel open)
"""
import os, pathlib, sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))   # repo root, so `agents.*` imports
os.environ.setdefault('AGENT_LLM', 'off')   # fast tests never call the hosted endpoint (the live tests can: AGENT_LLM=on)


def pytest_configure(config):
    config.addinivalue_line('markers', 'live: needs the SSH tunnel to the model, ./dev.sh and Neon (AGENTS_LIVE_RUN)')
