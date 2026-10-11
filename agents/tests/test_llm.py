"""llm.py: JSON extraction, retries, and the tool loop, with the model server replaced by scripted replies."""
import copy, json
import httpx
import pytest
from agents import llm


class Script:
    """Stands in for httpx.post: returns the scripted model messages in order and records the requests."""

    def __init__(self, *messages):
        self.messages, self.requests = list(messages), []

    def __call__(self, url, json=None, **kw):
        self.requests.append(copy.deepcopy(json))   # snapshot: run_agent keeps appending to the same list
        message = self.messages.pop(0)
        if isinstance(message, Exception):
            raise message
        return httpx.Response(200, json={'choices': [{'message': message}]}, request=httpx.Request('POST', url))


def call(name, **args):
    return {'id': f'call_{name}', 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}


@pytest.mark.parametrize('reply', ['{"a": 1}', '```json\n{"a": 1}\n```', 'Here you go: {"a": 1} done'])
def test_extract_json(reply):
    assert llm._extract_json(reply) == {'a': 1}


def test_ask_json_retries_until_json(monkeypatch):
    script = Script({'content': 'sorry, not json'}, {'content': '{"ok": true}'})
    monkeypatch.setattr(llm.httpx, 'post', script)
    assert llm.ask_json('system', 'user') == {'ok': True}
    assert script.requests[1]['messages'][-1]['content'].startswith('Reply again with ONLY')


def test_ask_json_gives_up_with_a_clear_error(monkeypatch):
    monkeypatch.setattr(llm.httpx, 'post', Script(*[{'content': 'nope'}] * 3))
    with pytest.raises(llm.LLMError, match='not JSON'):
        llm.ask_json('system', 'user')


def test_ask_json_sends_the_photo(monkeypatch):
    script = Script({'content': '{}'})
    monkeypatch.setattr(llm.httpx, 'post', script)
    llm.ask_json('system', 'user', image_url='http://x/p.jpg')
    content = script.requests[0]['messages'][1]['content']
    assert content[0] == {'type': 'image_url', 'image_url': {'url': 'http://x/p.jpg'}}


def test_run_agent_runs_tools_and_returns_the_final_answer(monkeypatch):
    script = Script({'content': '', 'tool_calls': [call('add', a=2, b=3)]}, {'content': '{"answer": 5}'})
    monkeypatch.setattr(llm.httpx, 'post', script)
    result = llm.run_agent('instructions', {'q': 1}, [], {'add': lambda a, b: {'sum': a + b}})
    assert result['output'] == {'answer': 5} and result['steps'] == 2
    assert result['tool_calls'] == [{'tool': 'add', 'args': {'a': 2, 'b': 3}, 'ok': True}]
    tool_message = script.requests[1]['messages'][-1]
    assert tool_message['role'] == 'tool' and json.loads(tool_message['content']) == {'sum': 5}


def test_run_agent_sends_tool_errors_back_to_the_model(monkeypatch):
    def failing(**kw):
        raise ValueError('building_id is not on this map')
    script = Script({'content': '', 'tool_calls': [call('upsert_incident', building_id='x')]},
                    {'content': '', 'tool_calls': [call('nonexistent')]},
                    {'content': '{"done": true}'})
    monkeypatch.setattr(llm.httpx, 'post', script)
    result = llm.run_agent('i', 'go', [], {'upsert_incident': failing})
    assert result['output'] == {'done': True}
    assert [c['ok'] for c in result['tool_calls']] == [False, False]
    assert 'not on this map' in script.requests[1]['messages'][-1]['content']
    assert 'unknown tool' in script.requests[2]['messages'][-1]['content']


def test_run_agent_asks_once_more_for_json(monkeypatch):
    monkeypatch.setattr(llm.httpx, 'post', Script({'content': 'All done!'}, {'content': '{"ok": 1}'}))
    assert llm.run_agent('i', 'go', [], {})['output'] == {'ok': 1}


def test_run_agent_stops_after_max_steps(monkeypatch):
    monkeypatch.setattr(llm.httpx, 'post', Script(*[{'content': '', 'tool_calls': [call('t')]}] * 3))
    with pytest.raises(llm.LLMError, match='no final answer after 3 steps'):
        llm.run_agent('i', 'go', [], {'t': lambda: {}}, max_steps=3)


def test_run_agent_truncates_huge_tool_results(monkeypatch):
    script = Script({'content': '', 'tool_calls': [call('big')]}, {'content': '{}'})
    monkeypatch.setattr(llm.httpx, 'post', script)
    llm.run_agent('i', 'go', [], {'big': lambda: 'x' * 50000})
    sent = script.requests[1]['messages'][-1]['content']
    assert len(sent) < llm.TOOL_RESULT_LIMIT + 100 and sent.endswith('ask for fewer items]')


def server_error(url, **kw):
    return httpx.Response(500, text='boom', request=httpx.Request('POST', url))


def test_server_error_names_the_step_after_retries(monkeypatch):
    calls = []
    monkeypatch.setattr(llm.time, 'sleep', lambda s: None)
    monkeypatch.setattr(llm.httpx, 'post', lambda url, **kw: calls.append(1) or server_error(url))
    with pytest.raises(llm.LLMError, match='server error 500 at step 1 .* 3 tries'):
        llm.run_agent('i', 'go', [], {})
    assert len(calls) == llm.SERVER_ERROR_RETRIES + 1


def test_a_momentary_server_error_keeps_the_agent_going(monkeypatch):
    """Seen live: a 500 at step 8 threw away detect's 7 tool calls. Now that step is retried."""
    replies = Script({'content': '', 'tool_calls': [call('t')]}, {'content': '{"done": true}'})
    errors = iter([True, False, False])   # second model turn fails once, then works
    monkeypatch.setattr(llm.time, 'sleep', lambda s: None)
    monkeypatch.setattr(llm.httpx, 'post', lambda url, **kw: server_error(url)
                        if len(replies.requests) == 1 and next(errors) else replies(url, **kw))
    result = llm.run_agent('i', 'go', [], {'t': lambda: {}})
    assert result['output'] == {'done': True} and result['tool_calls'][0]['ok']


def test_client_errors_are_not_retried(monkeypatch):
    calls = []
    monkeypatch.setattr(llm.httpx, 'post', lambda url, **kw: calls.append(1) or
                        httpx.Response(400, text='bad request', request=httpx.Request('POST', url)))
    with pytest.raises(llm.LLMError, match='server error 400'):
        llm.run_agent('i', 'go', [], {})
    assert len(calls) == 1
