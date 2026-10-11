'''
Shared connection to the team's language model (Qwen3-VL-8B on the AMD Radeon server).

Every AI agent calls ask_json(); nothing else in agents/ talks to the model directly, so switching to
another host (Hugging Face, Gemini's OpenAI endpoint, ...) only changes the three settings below.

    from agents.llm import ask_json
    data = ask_json(system="You read disaster reports...", user="Report: ...")      # -> dict
    data = ask_json(system=..., user=..., image_url="https://.../photo.jpg")        # vision

Settings (environment variables, else backend/.env, which is never committed):
    LLM_BASE_URL  http://localhost:8001/v1   (the SSH tunnel to the AMD server must be open:
                  ssh -N -o ServerAliveInterval=30 -L 8001:localhost:8000 root@36.150.116.206 -p 36756)
    LLM_API_KEY   none
    LLM_MODEL     Qwen/Qwen3-VL-8B-Instruct

The server speaks the OpenAI chat-completions API, called here with httpx (already installed).
'''
import json, os, pathlib, re, time
import httpx

ENV_FILE = pathlib.Path(__file__).resolve().parents[1] / 'backend' / '.env'


def setting(name, default=None):
    """Environment variable, else backend/.env, else default."""
    if os.environ.get(name):
        return os.environ[name]
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            if line.startswith(name + '='):
                return line.split('=', 1)[1].strip()
    return default


BASE_URL = setting('LLM_BASE_URL', 'http://localhost:8001/v1').rstrip('/')
API_KEY = setting('LLM_API_KEY', 'none')
MODEL = setting('LLM_MODEL', 'Qwen/Qwen3-VL-8B-Instruct')


class LLMError(RuntimeError):
    """The model could not be reached or never returned valid JSON."""


def _extract_json(text):
    """The JSON object in a reply, tolerating ```json fences or a sentence around it."""
    text = re.sub(r'^```(?:json)?|```$', '', text.strip(), flags=re.M).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find('{'), text.rfind('}')
        if start != -1 and end > start:
            return json.loads(text[start:end + 1])
        raise


def ask_json(system, user, image_url=None, max_tokens=500, retries=2, timeout=120):
    """Ask the model and return its answer parsed as a JSON object.

    temperature 0 keeps answers repeatable for the same report. A reply that is not valid JSON is
    retried with a reminder; connection errors are retried after a short pause.
    """
    content = user if not image_url else [
        {'type': 'image_url', 'image_url': {'url': image_url}},
        {'type': 'text', 'text': user}]
    messages = [{'role': 'system', 'content': system}, {'role': 'user', 'content': content}]
    last_error = None
    for attempt in range(retries + 1):
        try:
            r = httpx.post(f'{BASE_URL}/chat/completions', timeout=timeout,
                           headers={'Authorization': f'Bearer {API_KEY}'},
                           json={'model': MODEL, 'messages': messages, 'temperature': 0, 'max_tokens': max_tokens})
            r.raise_for_status()
            reply = r.json()['choices'][0]['message']['content']
        except (httpx.HTTPError, KeyError) as exc:
            last_error = f'model not reachable at {BASE_URL} ({exc}); is the SSH tunnel open?'
            time.sleep(2 * (attempt + 1))
            continue
        try:
            data = _extract_json(reply)
            if isinstance(data, dict):
                return data
            last_error = f'expected a JSON object, got {type(data).__name__}'
        except json.JSONDecodeError:
            last_error = f'reply was not JSON: {reply[:200]!r}'
        messages = messages + [{'role': 'assistant', 'content': reply},
                               {'role': 'user', 'content': 'Reply again with ONLY the JSON object, no other text.'}]
    raise LLMError(last_error)


# =============================================================================
# Agents with tools: the model decides which tool to call, code runs it, the model reads the result,
# and so on until it gives its final answer (an agent's .md file + its tools).
# =============================================================================

TOOL_RESULT_LIMIT = 8000   # characters of one tool result sent back to the model (keeps the context small)


def run_agent(instructions, user_input, tool_specs, tool_functions, max_steps=30, max_tokens=1500, timeout=180):
    """Run one agent invocation and return {"output": <final JSON answer>, "tool_calls": [...], "steps": n}.

    instructions    the agent's .md text (system prompt)
    user_input      dict (sent as JSON) or text: what this invocation is about
    tool_specs      tool descriptions in OpenAI format (agents/tools.py RunTools.toolbox)
    tool_functions  name -> Python function

    A tool that raises (e.g. ToolError: "building_id is not on this run's map") is not fatal: the error
    message goes back to the model so it can fix its input, as the .md files instruct.
    """
    user = user_input if isinstance(user_input, str) else json.dumps(user_input, ensure_ascii=False)
    messages = [{'role': 'system', 'content': instructions}, {'role': 'user', 'content': user}]
    trace, nudged = [], False
    for step in range(1, max_steps + 1):
        try:
            r = httpx.post(f'{BASE_URL}/chat/completions', timeout=timeout, headers={'Authorization': f'Bearer {API_KEY}'},
                           json={'model': MODEL, 'messages': messages, 'tools': tool_specs,
                                 'temperature': 0, 'max_tokens': max_tokens})
            r.raise_for_status()
            message = r.json()['choices'][0]['message']
        except httpx.HTTPStatusError as exc:
            raise LLMError(f'model server error {exc.response.status_code} at step {step} '
                           f'(after {len(trace)} tool calls): {exc.response.text[:300]}') from None
        except (httpx.HTTPError, KeyError) as exc:
            raise LLMError(f'model not reachable at {BASE_URL} ({exc}); is the SSH tunnel open?') from None

        calls = message.get('tool_calls') or []
        if not calls:   # no tool call: this is the final answer
            try:
                return {'output': _extract_json(message.get('content') or ''), 'tool_calls': trace, 'steps': step}
            except json.JSONDecodeError:
                if nudged:
                    raise LLMError(f'final answer was not JSON: {(message.get("content") or "")[:200]!r}')
                nudged = True
                messages += [{'role': 'assistant', 'content': message.get('content') or ''},
                             {'role': 'user', 'content': 'Give your final answer now as ONLY the JSON object.'}]
                continue

        messages.append({'role': 'assistant', 'content': message.get('content') or '', 'tool_calls': calls})
        for call in calls:
            name, args = call['function']['name'], None
            try:
                args = json.loads(call['function'].get('arguments') or '{}')
                if name not in tool_functions:
                    raise ValueError(f'unknown tool {name!r}; available: {sorted(tool_functions)}')
                result, ok = tool_functions[name](**args), True
            except Exception as exc:   # sent back to the model as the tool's answer
                result, ok = {'error': f'{type(exc).__name__}: {exc}'}, False
            trace.append({'tool': name, 'args': args, 'ok': ok, **({} if ok else {'error': result['error']})})
            text = json.dumps(result, ensure_ascii=False, default=str)
            if len(text) > TOOL_RESULT_LIMIT:
                text = text[:TOOL_RESULT_LIMIT] + ' ... [truncated: ask for fewer items]'
            messages.append({'role': 'tool', 'tool_call_id': call.get('id'), 'name': name, 'content': text})
    raise LLMError(f'no final answer after {max_steps} steps')
