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

import os
from dataclasses import dataclass
from pathlib import Path

import openai
from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv(Path(__file__).parent / ".env")

_client = None   # created by get_client(), then reused


class LLMUnavailable(Exception):
    """The Qwen server could not be reached after the SDK's retries."""


@dataclass
class Reply:
    """One assistant turn, flattened so the orchestrator can read it directly."""
    content: str | None      # the text reply (None when the model only calls tools)
    tool_calls: list | None  # OpenAI tool_call objects, or None
    finish_reason: str       # "tool_calls", "stop", or "length" (hit max_tokens)
    message: dict            # the assistant message, ready to append to `messages`


def get_client():
  """Return the shared AsyncOpenAI client, creating it on the first call.

  Reads QWEN_BASE_URL and QWEN_API_KEY. Raise a clear error if QWEN_BASE_URL is
  missing (most likely cause: agents/.env not loaded, or the SSH tunnel not open).
  """
  global _client
  if _client is not None:
    return _client
  base_url = os.getenv("QWEN_BASE_URL")
  if not base_url:
    raise RuntimeError(
      "QWEN_BASE_URL is not set. Check that agents/.env exists and defines it "
      "(e.g. http://localhost:8000/v1)."
    )
  api_key = os.getenv("QWEN_API_KEY") or "none"
  _client = AsyncOpenAI(
      base_url=base_url,
      api_key=api_key,
      timeout=120.0,   # seconds per request
      max_retries=2,   # SDK retries connection errors / 5xx with backoff
  )
  return _client



def get_model():
    """Return the model name to send with every request (QWEN_MODEL)."""
    model = os.getenv("QWEN_MODEL")
    if not model:
        raise RuntimeError(
            "QWEN_MODEL is not set. Check that agents/.env defines it "
            "(e.g. Qwen/Qwen3-VL-8B-Instruct)."
        )
    return model


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
    kwargs = {}
    if tools:
        # Only send tools when there are some; some servers reject "tools": null.
        kwargs["tools"] = tools

    try:
        response = await get_client().chat.completions.create(
            model=get_model(),
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            **kwargs,
        )
    except (openai.APIConnectionError, openai.InternalServerError) as e:
        # APIConnectionError also covers APITimeoutError. The SDK has already
        # retried by this point, so the tunnel or server is really down.
        raise LLMUnavailable(
            f"Qwen server unreachable at {os.getenv('QWEN_BASE_URL')} "
            f"(is the SSH tunnel open?): {e}"
        ) from e

    if not response.choices:
        raise LLMUnavailable("Qwen server returned no choices")

    choice = response.choices[0]
    msg = choice.message
    return Reply(
        content=msg.content,
        tool_calls=msg.tool_calls or None,
        finish_reason=choice.finish_reason,
        message=msg.model_dump(exclude_none=True),
    )
