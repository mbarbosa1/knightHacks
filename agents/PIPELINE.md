# Agent pipeline: agents → orchestrator → llm.py

```
run_pipeline(raw_input)                         orchestrator.py
   │
   ├─► run_agent("in-agent", handoff)     ─┐
   ├─► run_agent("detect-agent", handoff)  ├──► llm.chat(messages, tools) ──► Qwen server
   └─► run_agent("route-agent", handoff)  ─┘        llm.py                     (SSH tunnel)
```

An agent is not code that calls the orchestrator. An agent is just **a prompt file plus a
list of tool functions**. The orchestrator runs each agent and does all the calling; the
agent's only "actions" are the tool calls the model asks for.

## 1. What each agent gives the orchestrator

| Agent | System prompt | Tools | Status |
|---|---|---|---|
| in-agent | `assesmentAgent/in-agent.md` | its own list (TBD) | prompt empty |
| detect-agent | `assesmentAgent/detect-agent/detect-agent.md` | every function in `detect_tools.py` | written |
| route-agent | `responseAgent/route-agent.md` | its own list (TBD) | prompt empty |

Rules every agent's tools follow, so the orchestrator can treat them all the same:

- Signature starts with `(connection, run_id, ...)`. The orchestrator fills both in with
  `functools.partial`, so the model only supplies the rest (`lon`, `lat`, `radius_m`, ...)
  and can't reach another run.
- Return plain dicts/lists (UUIDs and datetimes are fine; they're serialized with `default=str`).
- The agent's final answer is **JSON in the message content**, and it becomes the next
  agent's handoff (e.g. detect-agent returns `run_id`, `cursor`, `situation_summary`,
  `incidents`, `road_restrictions`).
- On failure the output carries `"error": "neon_unavailable"` or `"llm_unavailable"`.

## 2. What the orchestrator does for one agent (`run_agent`)

1. `load_agent(name)` → `(system_prompt, tools)`.
2. Take one DB connection from the agents-role pool; `bind_tools(tools, connection, run_id)`.
3. `messages = [system prompt, user message = handoff as JSON]`.
4. Loop up to `MAX_TURNS` (15):
   - `reply = await llm.chat(messages, tool_schemas(tools))`
   - `reply.tool_calls` → append `reply.message`, run each call with `execute_tool_call`,
     append one `{"role": "tool", "tool_call_id": call.id, "content": result}` per call.
   - no tool calls → `json.loads(reply.content)` and return it.
5. Out of turns or bad JSON → retry once asking for JSON only, or return `{"error": ...}`.

`run_pipeline` chains the agents with plain `if`s: in → detect → route, stop on any
`"error"` (keep the old cursor so the caller retries), and skip route-agent when
detect-agent found no incidents and no road changes.

## 3. What the orchestrator uses from llm.py

| From llm.py | Used for |
|---|---|
| `chat(messages, tools=None, temperature=0.2, max_tokens=2048)` | the one call per turn |
| `Reply.content` | the final JSON answer (`None` when the model only called tools) |
| `Reply.tool_calls` | the tools to run this turn, or `None` |
| `Reply.finish_reason` | `"length"` means the answer was cut off at `max_tokens` |
| `Reply.message` | the assistant turn as a dict, appended to `messages` before tool results |
| `LLMUnavailable` | catch it in `run_agent` → return `{"error": "llm_unavailable"}` |

```python
try:
    reply = await llm.chat(messages, schemas)
except llm.LLMUnavailable:
    return {"error": "llm_unavailable"}
```

llm.py holds no agent logic. It keeps one shared `AsyncOpenAI` client (created on the first
call; it retries connection errors and 5xx twice with a 120s timeout) and reads
`QWEN_BASE_URL`, `QWEN_MODEL` and `QWEN_API_KEY` from `agents/.env`.
