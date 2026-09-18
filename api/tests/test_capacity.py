import asyncio
import json
from email.utils import format_datetime
from datetime import datetime, timezone, timedelta

import pytest

from router import Router, Failure, GROQ_MODEL, retry_seconds
from capacity import Capacity, LocalCapacity
from test_router import Response, completion, data, model, sse, tool
from test_recovery import Sequence, error, stream, stream_answer


async def test_worker_binding_uses_python_fetch_keyword_options():
    class WorkerResponse:
        status = 200

        async def text(self):
            return json.dumps(dict(ok=True, permit="reserved"))

    class Binding:
        def getByName(self, name):
            assert name == "global-v1"
            return self

        async def fetch(self, url, *, method, body):
            assert url == "https://capacity/capacity/reserve"
            assert method == "POST"
            assert json.loads(body) == dict(provider="openrouter", model="test/a:free", tokens=100)
            return WorkerResponse()

    assert await Capacity(Binding()).reserve("openrouter", "test/a:free", 100) == dict(ok=True, permit="reserved")


async def test_worker_binding_fails_closed_on_transport_error():
    class Binding:
        def getByName(self, name):
            raise RuntimeError("private transport error")

    with pytest.raises(Failure) as error:
        await Capacity(Binding()).reserve("openrouter", "test/a:free", 100)
    assert error.value.code == "capacity_unavailable"
    assert "private transport" not in str(error.value)


class Meter(LocalCapacity):
    def __init__(self, deny=None):
        self.calls, self.cooldowns, self.settlements = [], [], []
        self.deny = deny or {}

    async def reserve(self, provider, model, tokens):
        self.calls.append((provider, model, tokens))
        return self.deny.get(provider, dict(ok=True, permit=str(len(self.calls))))

    async def cooldown(self, provider, model, seconds):
        self.cooldowns.append((provider, model, seconds))

    async def settle(self, permit, tokens):
        self.settlements.append((permit, tokens))


async def no_wait(_):
    pass


@pytest.mark.parametrize('streaming', [True, False])
async def test_429_retries_same_model_then_changes_model_with_every_attempt_metered(streaming):
    stub = Sequence([error(429), error(429), stream_answer() if streaming else Response(completion())])
    meter, waits = Meter(), []
    async def sleep(seconds):
        waits.append(seconds)
    route = Router(stub, 'test', capacity=meter, sleep=sleep)
    if streaming:
        events = await stream(route, data(stream=True))
        result = events[-1]
        assert sum(e['type'] == 'done' for e in events) == 1
        assert any('Retrying in' in e.get('notice', '') for e in events)
    else:
        result = await route.chat(data())
    assert result['model'] == 'test/b:free'
    assert [b['model'] for b in stub.inferences()] == ['test/a:free', 'test/a:free', 'test/b:free']
    assert len(meter.calls) == 3
    assert len(waits) == 1 and 2 <= waits[0] <= 2.5
    assert meter.cooldowns == [('openrouter', 'test/a:free', 2), ('openrouter', 'test/a:free', 300)]
    assert len([u for u, _ in stub.calls if u.endswith('/models')]) == 3


async def test_pinned_429_can_retry_but_never_changes_model():
    stub = Sequence([error(429), stream_answer()])
    events = await stream(Router(stub, 'test', sleep=no_wait), data('test/a:free', stream=True))
    assert events[-1]['type'] == 'done'
    assert [b['model'] for b in stub.inferences()] == ['test/a:free'] * 2


async def test_provider_retry_after_is_not_shortened_and_long_delays_switch():
    busy = error(429, provider_name='provider'); busy.retry_after = '600'
    stub, waits, meter = Sequence([busy, Response(completion())]), [], Meter()
    async def sleep(seconds): waits.append(seconds)
    result = await Router(stub, 'test', capacity=meter, sleep=sleep).chat(data())
    assert result['model'] == 'test/b:free' and not waits
    assert meter.cooldowns == [('openrouter', 'test/a:free', 600)]
    busy = error(429); busy.retry_after = '7'
    stub = Sequence([busy, Response(completion())])
    await Router(stub, 'test', sleep=sleep).chat(data())
    assert len(waits) == 1 and 7 <= waits[0] <= 7.5


async def test_same_model_price_change_prevents_retry():
    stub = Sequence([error(429), Response(completion())])
    original = stub.request
    async def request(url, **kwargs):
        response = await original(url, **kwargs)
        if url.endswith('/chat/completions'):
            stub.catalog[0]['pricing']['prompt'] = '1'
        return response
    stub.request = request
    result = await Router(stub, 'test', sleep=no_wait).chat(data())
    assert result['model'] == 'test/b:free'
    assert [b['model'] for b in stub.inferences()] == ['test/a:free', 'test/b:free']


async def test_cancellation_during_backoff_never_sends_retry():
    waiting = asyncio.Event()
    async def sleep(_):
        waiting.set()
        await asyncio.Event().wait()
    stub = Sequence([error(429), stream_answer()])
    task = asyncio.create_task(stream(Router(stub, 'test', sleep=sleep), data(stream=True)))
    await waiting.wait(); task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert len(stub.inferences()) == 1


class Multiple(Sequence):
    def __init__(self, responses):
        super().__init__(responses)
        self.groq_calls = []

    async def request(self, url, **kwargs):
        if 'api.groq.com' in url:
            self.groq_calls.append((url, kwargs))
            assert kwargs['headers']['Authorization'] == 'Bearer groq-test'
            if url.endswith('/models'):
                return Response(dict(data=[dict(id='openai/gpt-oss-120b', active=True, context_window=131072)]))
            assert [t['function']['name'] for t in json.loads(kwargs['body'])['tools']] == ['bash']
            return Response(None, raw=sse(dict(choices=[dict(delta=dict(content='Independent pool'), finish_reason='stop')], x_groq=dict(usage=dict(total_tokens=500)))))
        assert kwargs.get('headers', {}).get('Authorization') != 'Bearer groq-test'
        return await super().request(url, **kwargs)


async def test_account_exhaustion_uses_independent_pool_and_settles_tokens():
    stub, meter = Multiple([error(429, limit_source='openrouter_daily')]), Meter()
    request = data(stream=True)
    request['messages'] += [dict(role='assistant', tool_calls=[tool()], reasoning_details=[dict(data='opaque')]), dict(role='tool', tool_call_id='call_a', content='already installed')]
    events = await stream(Router(stub, 'test', capacity=meter, groq_key='groq-test', groq_free=True, sleep=no_wait), request)
    assert events[-1]['model'] == GROQ_MODEL and events[-1]['type'] == 'done'
    assert len(stub.inferences()) == 1
    assert [c[0] for c in meter.calls] == ['openrouter', 'groq']
    assert meter.cooldowns == [('openrouter', None, 3600)]
    assert meter.settlements == [('1', None), ('2', 500)]
    body = json.loads(stub.groq_calls[-1][1]['body'])
    assert body['messages'][-1]['content'] == 'already installed'
    assert 'reasoning_details' not in body['messages'][-2]
    assert 'provider' not in body and body['max_completion_tokens'] == 2048


async def test_groq_is_disabled_without_explicit_free_account_configuration():
    stub = Multiple([stream_answer()])
    events = await stream(Router(stub, 'test', groq_key='groq-test'), data(stream=True))
    assert events[-1]['type'] == 'done' and not stub.groq_calls


async def test_secondary_catalog_failure_during_recovery_keeps_other_routes_available():
    stub = Multiple([error(503), stream_answer()])
    original = stub.request
    catalogs = 0

    async def request(url, **kwargs):
        nonlocal catalogs
        if 'api.groq.com' in url and url.endswith('/models'):
            catalogs += 1
            if catalogs > 1:
                raise TimeoutError()
        return await original(url, **kwargs)

    stub.request = request
    events = await stream(Router(stub, 'test', groq_key='groq-test', groq_free=True), data(stream=True))
    assert events[-1]['type'] == 'done' and events[-1]['model'] == 'test/b:free'
    assert catalogs == 2
    assert not any(url.endswith('/chat/completions') for url, _ in stub.groq_calls)


async def test_local_quota_uses_other_provider_without_sending_rejected_attempt():
    meter = Meter(dict(openrouter=dict(ok=False, code='provider_capacity', retry_after_seconds=60)))
    stub = Multiple([])
    events = await stream(Router(stub, 'test', capacity=meter, groq_key='groq-test', groq_free=True, sleep=no_wait), data(stream=True))
    assert events[-1]['model'] == GROQ_MODEL and not stub.inferences()


async def test_all_capacity_exhausted_returns_honest_retry_time_no_inference():
    meter = Meter(dict(openrouter=dict(ok=False, code='provider_capacity', retry_after_seconds=60)))
    stub = Sequence([])
    events = await stream(Router(stub, 'test', capacity=meter, sleep=no_wait), data(stream=True))
    assert events[-1]['code'] == 'free_capacity_exhausted'
    assert events[-1]['retry_after_seconds'] == 60 and not stub.inferences()


def test_retry_after_supports_http_date_and_rejects_malformed():
    assert retry_seconds('7') == 7
    assert retry_seconds('0.5') == 1
    assert retry_seconds('nonsense') is None
    assert 8 <= retry_seconds(format_datetime(datetime.now(timezone.utc) + timedelta(seconds=10))) <= 10
