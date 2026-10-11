'''
Runs the agents in order for one event (plain Python, not an LLM)

orchestrator.py  The pipeline order is fixed, so code decides what runs next:

    in-agent  ──►  detect-agent 
    (normalize)    (assess)           

Every agent is the same shared model (llm.py) with a different system prompt (its .md
file) and a different set of tools. run_agent() is the one tool loop they all share;
run_pipeline() chains them with ordinary if-statements.

What gets created when:
  once per process     the LLM client (llm.py), the agents-role database pool
  once per invocation  a fresh message list, one DB connection, tools bound to run_id

Settings, read from agents/.env:
  QWEN_BASE_URL / QWEN_MODEL / QWEN_API_KEY  (used by llm.py)
  AGENTS_DATABASE_URL                        the agents-role Neon login (not added yet)

Sections in this file:
  1. Setup        (database pool, agent registry)
  2. Tools        (bind to the event, describe to the model, run a call)
  3. Agent loop   (run_agent)
  4. Pipeline     (run_pipeline)
'''

from pathlib import Path

AGENTS_DIR = Path(__file__).parent

# Name -> system prompt file. Tools per agent are listed in load_agent().
AGENT_PROMPTS = {
    "in-agent":     AGENTS_DIR / "assesmentAgent" / "in-agent.md",
    "detect-agent": AGENTS_DIR / "assesmentAgent" / "detect-agent" / "detect-agent.md",
    "route-agent":  AGENTS_DIR / "responseAgent" / "route-agent.md",
}

MAX_TURNS = 15   # model calls per invocation before giving up


# ============================================================================
# 1. Setup
# ============================================================================

async def get_pool():
    """Return the shared database pool for the agents role, creating it on first call.

    Use psycopg_pool.AsyncConnectionPool with AGENTS_DATABASE_URL and
    row_factory=dict_row (same as backend/app/db.py), so tools get rows as dicts.
    Never use the owner or simulator login here.
    """
    raise NotImplementedError


def load_agent(name):
    """Return (system_prompt, tools) for one agent.

    - system_prompt: the text of AGENT_PROMPTS[name].
    - tools: the list of tool functions this agent may call, e.g. detect-agent gets
      every function in detect_tools.py. in-agent and route-agent get their own lists.
    - Unknown name -> raise ValueError.
    Note: the folder `detect-agent` has a hyphen, so `import detect_tools` from here
    needs that folder renamed (detect_agent) or the tools moved somewhere importable.
    """
    raise NotImplementedError


# ============================================================================
# 2. Tools
# ============================================================================

def bind_tools(tools, connection, run_id):
    """Return {tool_name: callable} with connection and run_id already filled in.

    functools.partial(tool, connection, run_id). After this, the model only supplies
    the remaining arguments (lon, lat, radius_m, ...), so it can't reach another run.
    """
    raise NotImplementedError


def tool_schemas(tools):
    """Return the OpenAI `tools` list describing each tool to the model.

    One {"type": "function", "function": {"name", "description", "parameters"}} per
    tool. Build it from each function's signature and docstring (skipping connection
    and run_id), or write the schemas by hand if you want tighter descriptions.
    """
    raise NotImplementedError


async def execute_tool_call(tool_call, bound_tools):
    """Run one tool call from the model and return its result as a JSON string.

    - Parse tool_call.function.arguments (a JSON string) into kwargs.
    - Unknown tool name, bad JSON or wrong arguments -> {"error": "..."} explaining
      what was wrong, so the model can fix it on the next turn.
    - Exceptions from the tool -> {"error": "..."} too; never crash the loop.
    - json.dumps(result, default=str) so UUIDs and datetimes serialize.
    """
    raise NotImplementedError


# ============================================================================
# 3. Agent loop
# ============================================================================

async def run_agent(name, handoff):
    """Run one agent on one event; return its final JSON output.

    1. load_agent(name); take one connection from get_pool().
    2. bind_tools(tools, connection, handoff["run_id"]).
    3. messages = [system prompt, user message containing the handoff as JSON].
    4. Loop up to MAX_TURNS:
         reply = await llm.chat(messages, tool_schemas(tools))
         - reply has tool_calls -> append the assistant message, then one
           {"role": "tool", "tool_call_id": ..., "content": result} per call.
         - no tool_calls -> parse reply.content as JSON and return it.
    5. Hit MAX_TURNS, or content isn't valid JSON -> decide: ask the model once more
       for JSON only, or return {"error": "..."}.
    Also time the run so log_agent_run (or this function) can record duration_ms.
    """
    raise NotImplementedError


# ============================================================================
# 4. Pipeline
# ============================================================================

async def run_pipeline(raw_input):
    """Run in-agent -> detect-agent -> route-agent for one event; return all outputs.

    raw_input carries at least the run_id (and the new observation ids, if known).
    Plain rules decide the flow, e.g.:
      - in-agent output -> detect-agent handoff.
      - any output with "error" (neon_unavailable / llm_unavailable) -> stop, keep
        the old cursor, and return so the caller can retry later.
      - detect-agent found no incidents and no road changes -> skip route-agent.
    Return {"in": ..., "detect": ..., "route": ...} for logging and debugging.
    """
    raise NotImplementedError
