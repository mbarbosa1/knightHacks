# Agent pipeline: agents → orchestrator → llm.py

```
orchestrator.py   Orchestrator(run_id).cycle()  /  --watch
   │
   ├─ stage 1  InAgent.process(report)            one per new report, 6 in parallel
   │              └─► llm.ask_json(...)           one model call, no tool loop
   │
   └─ stage 2  detect ‖ medical ‖ verify          same batch of 5 records, in parallel
                  └─► llm.run_agent(...)          tool loop: model ⇄ agents/tools.py
                                                        │
                                       httpx POST /chat/completions
                                                        ▼
                         localhost:8001 ══ SSH tunnel ══► GPU box :8000 (Qwen3-VL-8B)
```

The orchestrator does no AI work. It decides which agent runs when and passes each one's
output to the next. An agent is **an instructions `.md` file + a list of tool names**; the
only Python agent is `in_agent.py`, because its tools are called by code, not by the model.

## Ports

| Port | Where | What |
|---|---|---|
| **8000** | GPU box | the Qwen model server (`transformers serve`). Stays on 8000. |
| 8001 | your laptop | local end of the SSH tunnel → GPU box :8000. `llm.py` calls this. |
| 8000 | your laptop | the backend API from `./dev.sh`. `tools.py` reads runs and reports here. |

Open the tunnel before running any agent:

```bash
ssh -N -o ServerAliveInterval=30 -L 8001:localhost:8000 root@36.150.116.206 -p 36756
```

The two 8000s don't clash: one is on the GPU box, the other on your laptop. Pointing
`LLM_BASE_URL` at `localhost:8000` would send model requests to the backend instead.

## 1. How each agent plugs into the orchestrator

| Agent | Instructions | Tools | How it runs |
|---|---|---|---|
| in_agent | `assesmentAgent/in-agent.md` | `find_nearby_buildings`, `find_nearest_road` (called by `in_agent.py`) | `InAgent.process(obs)` → `ask_json` |
| detect_agent | `assesmentAgent/detect-agent/detect-agent.md` | read + `upsert_incident`, `set_road_restriction`, `log_agent_run` | `run_agent` |
| medical_agent | `assesmentAgent/medical-agent.md` | read + `upsert_incident`, `log_agent_run` | `run_agent` |
| verify_agent | `assesmentAgent/verify-agent.md` | read + `find_nearby_buildings`, `upsert_incident`, `log_agent_run` | `run_agent` |
| resource_agent | `responseAgent/resource-agent.md` | read + `list_resources`, `list_decisions`, `nearest_units`, `assign_units`, `facility_options`, `log_agent_run` | not wired up yet |
| route_agent | `responseAgent/route-agent.md` | read + `list_road_restrictions`, `list_decisions`, `check_route`, `plan_route`, `facility_options`, `invalidate_decision`, `log_agent_run` | not wired up yet |
| plan_agent | `responseAgent/plan-agent.md` | read + `list_resources`, `list_decisions`, `plan_route`, `upsert_decision`, `log_agent_run` | not wired up yet |
| report_agent | `responseAgent/report-agent.md` | read + `list_road_restrictions`, `list_resources`, `list_decisions`, `log_agent_run` | not wired up yet |

The tool lists live in `ASSESSMENT_AGENTS` in `orchestrator.py`. An agent takes part once its
`.md` exists at that path; until then it's skipped, so agents can be added one at a time.
Try a draft without moving it: `--instructions detect_agent=/path/to/detect-agent.md`.

What an agent's `.md` can rely on:

- **Input:** `{"run_id", "observation_ids", "cursor", "normalized": [in_agent records]}`.
  The record format is in `in-agent.md` under "For developers".
- **Tools** come from `RunTools` in `agents/tools.py`, already scoped to the run, so the
  model never passes a `run_id`. `get_last_agent_run` and `log_agent_run` are also bound to
  the agent's name by `RunTools.toolbox()`.
- **A tool that raises** (`ToolError`, bad arguments, unknown tool) is not fatal: the error
  goes back to the model as the tool result so it can fix its input.
- **Final answer:** a JSON object in the message content, with no further tool calls.
  The orchestrator reads `incidents` and `situation_summary` from it.

## 2. What the orchestrator does

1. `new_reports()`: every report in the run that `in_agent` hasn't processed yet, oldest first.
2. `normalize()`: `InAgent.process` on each, in parallel. Failed reports are dropped and
   retried next cycle.
3. `assess()`: for each batch of 5 records, runs every existing assessment agent in parallel:
   ```python
   specs, functions = self.tools.toolbox(name, tool_names)
   result = run_agent(instructions, payload, specs, functions)
   ```
   One agent failing doesn't stop the others; its result becomes `{"error": ...}`.
4. Logs each batch to Neon `ops.agent_runs` (agent `orchestrator`) and moves the cursor on.

```bash
.venv/bin/python -m agents.orchestrator <run_id>            # everything new, then stop
.venv/bin/python -m agents.orchestrator <run_id> --watch    # live loop, checks every 10s
```

## 3. What the orchestrator uses from llm.py

| From llm.py | Used by | Does |
|---|---|---|
| `ask_json(system, user, image_url=None)` | `in_agent.py` | one call → parsed JSON dict; retries bad JSON and dropped connections twice |
| `run_agent(instructions, user_input, tool_specs, tool_functions)` | orchestrator stage 2 | tool loop, up to 30 steps → `{"output", "tool_calls", "steps"}` |
| `LLMError` | both | model unreachable, server error, or never returned valid JSON |
| `setting(name, default)` | `llm.py`, `tools.py` | environment variable, else `backend/.env`, else default |

`llm.py` is synchronous (`httpx`); parallelism comes from the orchestrator's thread pools.
It runs at temperature 0 and cuts tool results at 8000 characters.

Settings (`backend/.env` or environment; the defaults already match the tunnel above):

```
LLM_BASE_URL=http://localhost:8001/v1
LLM_API_KEY=none
LLM_MODEL=Qwen/Qwen3-VL-8B-Instruct
```

`agents/.env` and its `QWEN_*` settings are no longer read by anything.

## Not wired up yet

- Response agents (resource ‖ route → plan → report) have instructions but aren't in the
  orchestrator yet. Each `.md` documents its input: the assessment agents' outputs plus
  `incident_ids` (incidents changed in the batch); plan gets resource + route outputs; report gets
  everything.
- They need these tools added to `RunTools` / `TOOL_SPECS` in `agents/tools.py` (same rules as the
  existing write tools: run must be live, ids checked, one `ops.operational_events` row per change):

  | Tool | Does | Built on |
  |---|---|---|
  | `list_resources(status?, type?)` | units: id, type (`ambulance` / `fire_engine` / `police`), status, location, home facility | `GET /runs/{id}/resources` |
  | `list_decisions(status?)` | plans with their assignments, routes, version, status | `ops.decisions` |
  | `nearest_units(incident_id, type?, k?)` | fastest available units to an incident | `Router.nearest_units` |
  | `assign_units(incident_ids, type)` | one unit per incident, minimum total drive time | `Router.assign` |
  | `facility_options(lon, lat, type)` | facilities ranked by drive time | `Router.facility_options` |
  | `plan_route(origin, destination)` | route + fallback; places are `{incident_id}` / `{unit_id}` / `{facility_id}` / `{longitude, latitude}` | `Router.route` |
  | `check_route(decision_id)` | roads on a plan's route that are no longer OPEN | `Router.affected_edges` (store `edge_ids` with the route) |
  | `upsert_decision(incident_id, assignments, route, fallback_route, eta_s)` | new `draft` version for the incident; never approved/sent | `ops.decisions` |
  | `invalidate_decision(decision_id, reason)` | status `invalidated` + `invalidated_reason` | `ops.decisions` |

  Every routing tool plans on the agents' beliefs (`list_road_restrictions`), never on truth.
- `assesmentAgent/detect-agent/detect_tools.py` is replaced by `agents/tools.py`.
