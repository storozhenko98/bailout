import json
from copy import deepcopy
from decimal import Decimal

import httpx
import pytest

from app import app
from router import Failure, Router, check_message, free_model, free_pricing, healthy, validate, zero


def model(id='test/coder:free'):
    return dict(id=id, name=id, context_length=128000, description='Agentic coding model', pricing=dict(prompt='0', completion='0'), supported_parameters=['tools', 'max_tokens'])


def endpoint(id='test/coder:free'):
    return dict(model_id=id, status=0, tag='provider/fp16', context_length=128000,
                pricing=dict(prompt='0', completion='0', discount=0), supported_parameters=['tools', 'max_tokens'],
                max_completion_tokens=8192, uptime_last_30m=99.9, uptime_last_5m=100)


def tool():
    return dict(id='call_a', type='function', function=dict(name='bash', arguments='{"command":"pwd"}'))


def completion(message=None):
    return dict(choices=[dict(finish_reason='stop', message=message or dict(role='assistant', content='Hello!'))], usage=dict(cost=0))


def data(selected='auto', stream=False):
    return validate(dict(model=selected, messages=[dict(role='user', content='hello')], stream=stream))


class Response:
    def __init__(self, value, status=200, raw=None):
        self.status, self.closed = status, False
        self.raw = raw if raw is not None else json.dumps(value).encode()
    async def chunks(self):
        for offset in range(0, len(self.raw), 7):
            yield self.raw[offset:offset+7]
    async def close(self):
        self.closed = True


class Upstream:
    def __init__(self, catalog=None, patch=None, status=200, answer=None):
        self.catalog = catalog or [model()]
        self.patch, self.status, self.answer = patch or {}, status, answer or completion()
        self.calls, self.responses = [], []
    async def request(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url.endswith('/models'):
            response = Response(dict(data=self.catalog))
        elif url.endswith('/endpoints'):
            id = url.split('/models/')[1].removesuffix('/endpoints')
            response = Response(dict(data=dict(id=id, endpoints=[{**endpoint(id), **self.patch}])))
        else:
            response = Response(self.answer, self.status)
        self.responses.append(response)
        return response
    def inferences(self):
        return [json.loads(k['body']) for u, k in self.calls if u.endswith('/chat/completions')]


@pytest.mark.parametrize('value', ['1e-999', '', ' ', None, False, -1, 'NaN', 'free', Decimal('1e-1000'), '0.000001'])
def test_price_rejects_nonzero_and_unknown(value):
    assert not zero(value)


def test_zero_and_all_reported_fees():
    assert all(zero(v) for v in ['0', '0.000', '0e-100', 0])
    assert free_pricing(dict(prompt='0', completion='0'))
    for p in [dict(prompt='0'), dict(prompt=0, completion=0), dict(prompt='0', completion='0', request='1e-999'), dict(prompt='0', completion='0', future_fee=None)]:
        assert not free_pricing(p)


def test_only_explicit_free_and_healthy():
    for id in ['openrouter/auto', 'openrouter/free', 'test/paid', 'test/a:free:nitro', 'test/a:free/../paid']:
        assert not free_model(model(id))
    assert free_model(model())  # tools work without support for forced tool_choice
    assert healthy(endpoint(), 'test/coder:free')
    for patch in [dict(status=-1), dict(uptime_last_30m=None), dict(uptime_last_5m=0), dict(tag=''), dict(supported_parameters=[]), dict(model_id='different/model:free')]:
        assert not healthy({**endpoint(), **patch}, 'test/coder:free')


@pytest.mark.asyncio
async def test_normal_conversation_never_requires_bash_and_checks_every_request():
    stub = Upstream()
    route = Router(stub, 'test-key')
    for _ in range(2):
        assert (await route.chat(data()))['message']['content'] == 'Hello!'
    assert len([u for u, _ in stub.calls if u.endswith('/models')]) == 2
    assert len([u for u, _ in stub.calls if u.endswith('/endpoints')]) == 2
    for body in stub.inferences():
        assert 'tool_choice' not in body
        assert [t['function']['name'] for t in body['tools']] == ['bash']
        assert body['provider'] == dict(only=['provider/fp16'], allow_fallbacks=False, require_parameters=True, max_price=dict(prompt=0, completion=0, request=0, image=0))
        assert not {'models', 'plugins'} & set(body)
    assert all(r.closed for r in stub.responses)


@pytest.mark.asyncio
async def test_price_change_between_turns_fails_closed():
    stub = Upstream()
    route = Router(stub, 'test-key')
    await route.chat(data())
    stub.catalog[0]['pricing']['completion'] = '0.01'
    with pytest.raises(Failure): await route.chat(data())
    assert len(stub.inferences()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('patch', [dict(pricing=dict(prompt='0', completion='0', request='0.01')), dict(status=1), dict(uptime_last_30m=50)])
async def test_paid_or_unhealthy_endpoint_never_called(patch):
    stub = Upstream(patch=patch)
    with pytest.raises(Failure): await Router(stub, 'test-key').chat(data())
    assert not stub.inferences()


def test_overrides_multimodal_and_invalid_history():
    for extra in [dict(provider=dict(max_price=dict(prompt=10))), dict(plugins=[dict(id='web')]), dict(models=['paid/model']), dict(model='paid/model'), dict(messages=[dict(role='user', content=[dict(type='image_url')])])]:
        with pytest.raises(Failure): validate({**data(), **extra})
    call = dict(role='assistant', content=None, tool_calls=[tool()])
    with pytest.raises(Failure): validate(dict(messages=[call]))
    validate(dict(messages=[call, dict(role='tool', content='ok', tool_call_id='call_a')]))
    call['tool_calls'][0]['function']['name'] = 'read_file'
    with pytest.raises(Failure): validate(dict(messages=[call]))


@pytest.mark.asyncio
async def test_fallback_rechecks_free_prices_and_pinned_model_stays_pinned():
    stub = Upstream(catalog=[model('test/a:free'), model('test/b:free'), model('test/paid')], status=429)
    original = stub.request
    async def request(url, **kwargs):
        response = await original(url, **kwargs)
        if url.endswith('/chat/completions'):
            stub.catalog[1]['pricing']['prompt'] = '0.01'
        return response
    stub.request = request
    with pytest.raises(Failure): await Router(stub, 'test-key').chat(data())
    assert [b['model'] for b in stub.inferences()] == ['test/a:free']
    pinned = Upstream(catalog=[model('test/a:free'), model('test/b:free')], status=429)
    with pytest.raises(Failure): await Router(pinned, 'test-key').chat(data('test/a:free'))
    assert len(pinned.inferences()) == 1


def test_malformed_and_cost_audit():
    for value in [dict(choices=[], usage=dict(cost=.1)), dict(choices=[dict(finish_reason='length')]), completion(dict(role='assistant', content=None)), completion(dict(role='assistant', tool_calls=[{**tool(), 'function':dict(name='bash', arguments='{')}]))]:
        with pytest.raises(Failure): check_message(value)
    assert check_message(completion(dict(role='assistant', tool_calls=[tool()])))['tool_calls']


def sse(*frames, done=True):
    return (''.join('data: '+json.dumps(f, ensure_ascii=False)+'\n\n' for f in frames) + ('data: [DONE]\n\n' if done else '')).encode()


@pytest.mark.asyncio
async def test_real_stream_chunks_utf8_tool_assembly_and_validation():
    raw = sse(dict(choices=[dict(delta=dict(content='Hello 🌍.'))]),
              dict(choices=[dict(delta=dict(tool_calls=[dict(index=0, id='a', type='function', function=dict(name='bash', arguments='{"com'))]))]),
              dict(choices=[dict(delta=dict(tool_calls=[dict(index=0, function=dict(arguments='mand":"pwd"}'))]), finish_reason='tool_calls')], usage=dict(cost=0)))
    response = Response(None, raw=raw)
    events = [json.loads(e) async for e in Router(None, '').stream('test/coder:free', response)]
    assert events[1] == dict(type='text', text='Hello 🌍.')
    assert events[-1]['type'] == 'done'
    assert events[-1]['message']['tool_calls'][0]['function']['arguments'] == '{"command":"pwd"}'
    assert response.closed


@pytest.mark.asyncio
@pytest.mark.parametrize('raw', [sse(dict(choices=[dict(delta=dict(content='partial'))]), done=False), sse(dict(choices=[dict(delta=dict(content='partial'), finish_reason='length')])), sse(dict(choices=[dict(delta=dict(content='hello'), finish_reason='stop')], usage=dict(cost=.01))), b'data: broken\n\n'])
async def test_stream_never_completes_on_truncation_cost_or_invalid_json(raw):
    response = Response(None, raw=raw)
    events = [json.loads(e) async for e in Router(None, '').stream('test/coder:free', response)]
    assert events[-1]['type'] == 'error'
    assert not any(e['type'] == 'done' for e in events)
    assert response.closed


@pytest.mark.asyncio
async def test_fastapi_routes_limits_and_errors(monkeypatch):
    stub = Upstream()
    monkeypatch.setenv('OPENROUTER_API_KEY', 'test-secret-not-real')
    app.state.transport = stub
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            assert (await client.get('/health')).json()['framework'] == 'FastAPI'
            assert (await client.get('/docs')).status_code == 200
            assert '/v1/chat' in (await client.get('/openapi.json')).json()['paths']
            result = await client.post('/v1/chat', json=data())
            assert result.status_code == 200
            assert result.json()['message']['content'] == 'Hello!'
            assert (await client.post('/v1/chat', content='{')).status_code == 400
            before = len(stub.calls)
            assert (await client.post('/v1/chat', json=dict(messages=[dict(role='user', content='x'*513000)]))).status_code == 413
            assert len(stub.calls) == before
            stub.status = 401
            stub.answer = dict(error='test-secret-not-real')
            result = await client.post('/v1/chat', json=data())
            assert result.status_code == 503
            assert 'test-secret-not-real' not in result.text
    finally:
        app.state.transport = None


def test_interactive_bash_is_explicit_and_type_checked():
    call = tool()
    call['function']['arguments'] = json.dumps(dict(command='gh auth login', interactive=True))
    assert check_message(completion(dict(role='assistant', tool_calls=[call])))['tool_calls']
    call['function']['arguments'] = json.dumps(dict(command='gh auth login', interactive='yes'))
    with pytest.raises(Failure): check_message(completion(dict(role='assistant', tool_calls=[call])))
