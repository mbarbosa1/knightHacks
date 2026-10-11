'''
The one shared connection to the Qwen server

llm.py  Every agent talks to the same model through this file. The client is created
once (first call to get_client) and reused for every agent invocation; nothing here
reloads the model. The model itself runs on the AMD GPU box (`transformers serve`,
OpenAI-compatible API), reached through the SSH tunnel on localhost.

Settings, read from agents/.env:
  QWEN_BASE_URL  e.g. http://localhost:8000/v1 (the tunnel)
  QWEN_MODEL     e.g. Qwen/Qwen3-VL-8B-Instruct
  QWEN_API_KEY   "none" while the server has no --api-key

Plan:
  - Use openai.AsyncOpenAI pointed at QWEN_BASE_URL; it already speaks the
    /v1/chat/completions format the server returned in the tool-call test.
  - Load agents/.env with python-dotenv so this works no matter where it's run from.
  - Keep this file free of agent logic: it only sends messages and returns replies.
'''

_client = None   # created by get_client(), then reused


def get_client():
    """Return the shared AsyncOpenAI client, creating it on the first call.

    Reads QWEN_BASE_URL and QWEN_API_KEY. Raise a clear error if QWEN_BASE_URL is
    missing (most likely cause: agents/.env not loaded, or the SSH tunnel not open).
    """
    raise NotImplementedError


def get_model():
    """Return the model name to send with every request (QWEN_MODEL)."""
    raise NotImplementedError


async def chat(messages, tools=None, temperature=0.2, max_tokens=2048):
    """Send one chat completion request and return the assistant message.

    - messages: the OpenAI-format list (system / user / assistant / tool).
    - tools:    OpenAI tool schemas, or None for a plain reply.
    - Return the message object (content + tool_calls) from choices[0], plus
      finish_reason ("tool_calls" means the model wants tools run; "stop" means done).
    - Low temperature: the agents should be consistent, not creative.
    - Decide on a timeout and how many retries for connection errors (tunnel dropped,
      server restarting). Raise a specific error after the last retry so the
      orchestrator can return {"error": "llm_unavailable"}.
    """
    raise NotImplementedError
