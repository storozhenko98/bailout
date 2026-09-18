"""Production queue integration: waits never manufacture capacity or skip policy."""
import asyncio
import json

import pytest
import anyio

from capacity import Capacity
from router import Router, Failure
from test_capacity import Meter
from test_router import Upstream, Response, completion, data, model
from test_recovery import Sequence, error, stream, stream_answer


class Queued(Meter):
    fair_queue = True

    def __init__(self, polls=2):
        super().__init__()
        self.polls = polls
        self.ready = False
        self.started = None
        self.closed = False
        self.removed = []

    async def start(self, routes):
        self.started = routes

    async def reserve(self, provider, model, tokens):
        if not self.ready:
            return {"ok": False, "code": "queue_wait", "scope": "provider", "retry_after_seconds": 2}
        return await super().reserve(provider, model, tokens)

    async def waiting(self):
        self.polls -= 1
        self.ready = self.polls <= 0
        return {"ok": self.ready, "retry_after_seconds": 2}

    async def close(self):
        self.closed = True

    async def discard(self, **route):
        self.removed.append(route)


async def test_wait_emits_status_and_rechecks_fresh_price_without_polling_the_provider():
    meter, stub, waits = Queued(4), Sequence([stream_answer()]), []
    stub.catalog = [model('test/a:free')]

    async def sleep(delay):
        waits.append(delay)
        # All three queue polls see exactly one eligibility refresh. The first
        # /models is initial discovery; the second is the attempted admission.
        assert len([url for url, _ in stub.calls if url.endswith('/models')]) == 2
        assert not stub.inferences()

    route = Router(stub, 'test', capacity=meter, sleep=sleep)
    events = await stream(route, data(stream=True))
    assert events[-1]['type'] == 'done'
    assert any(e.get('reason') == 'capacity_queue' for e in events)
    assert len(waits) == 3 and all(5 <= delay <= 5.25 for delay in waits)
    assert len([url for url, _ in stub.calls if url.endswith('/models')]) == 3
    assert len(stub.inferences()) == 1 and len(meter.settlements) == 1
    assert meter.closed and len(meter.started) == 1
    assert set(meter.started[0]) == {'provider', 'model', 'tokens'}


async def test_a_model_that_becomes_paid_while_waiting_is_never_called():
    meter, stub = Queued(), Upstream(catalog=[model('test/a:free')])

    async def sleep(_):
        stub.catalog[0]['pricing']['prompt'] = '1'

    events = await stream(Router(stub, 'test', capacity=meter, sleep=sleep), data(stream=True))
    assert events[-1]['type'] == 'error'
    assert not stub.inferences()
    assert meter.closed and {'model': 'test/a:free'} in meter.removed


async def test_cancelled_wait_releases_ticket_without_sending_an_inference():
    meter, stub, asleep = Queued(100), Upstream(), asyncio.Event()

    async def sleep(_):
        asleep.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(stream(Router(stub, 'test', capacity=meter, sleep=sleep), data(stream=True)))
    await asleep.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert meter.closed and not stub.inferences()


async def test_queue_wait_has_a_bound_and_preserves_structured_client_retry():
    meter, stub, delays = Queued(1000), Upstream(), []

    async def sleep(delay):
        delays.append(delay)

    events = await stream(Router(stub, 'test', capacity=meter, sleep=sleep), data(stream=True))
    assert sum(delays) == pytest.approx(90)
    assert events[-1]['code'] == 'free_capacity_exhausted'
    assert events[-1]['retry_after_seconds'] == 5
    assert 'after waiting' in events[-1]['error']
    assert not stub.inferences() and meter.closed


async def test_asgi_disconnect_cancellation_scope_still_removes_waiting_ticket():
    class Cleanup(Queued):
        async def close(self):
            await anyio.sleep(0)  # A real service-binding call must be awaited.
            self.closed = True
    meter, stub, asleep = Cleanup(100), Upstream(), anyio.Event()

    async def sleep(_):
        asleep.set()
        await anyio.sleep_forever()

    async with anyio.create_task_group() as group:
        group.start_soon(stream, Router(stub, 'test', capacity=meter, sleep=sleep), data(stream=True))
        await asleep.wait()
        group.cancel_scope.cancel()
    assert meter.closed and not stub.inferences()


async def test_waiting_does_not_restart_the_four_attempt_or_same_model_retry_budget():
    class BetweenAttempts(Queued):
        def __init__(self):
            super().__init__(1)
            self.ready = True

        async def reserve(self, provider, model, tokens):
            response = await super().reserve(provider, model, tokens)
            if response['ok']:
                self.ready = False
            return response

        async def waiting(self):
            self.ready = True
            return {'ok': True}

    meter, stub = BetweenAttempts(), Sequence([error(429) for _ in range(8)])

    async def sleep(_):
        pass

    events = await stream(Router(stub, 'test', capacity=meter, sleep=sleep), data(stream=True))
    assert events[-1]['type'] == 'error'
    assert len(stub.inferences()) == 4
    selected = [body['model'] for body in stub.inferences()]
    assert all(selected.count(model_id) <= 2 for model_id in selected)
    assert len(meter.settlements) == 4 and meter.closed


class Binding:
    def __init__(self):
        self.calls = []
        self.joined = set()
        self.full = False

    def getByName(self, _):
        return self

    async def fetch(self, url, *, method, body):
        action, args = url.rsplit('/', 1)[-1], json.loads(body)
        self.calls.append((action, args))
        if action == 'join':
            if self.full:
                result = {'ok': False, 'code': 'queue_full', 'retry_after_seconds': 15}
            else:
                self.joined.add(args['ticket'])
                result = {'ok': True}
        elif action == 'reserve':
            self.joined.remove(args['ticket'])
            result = {'ok': True, 'permit': 'permit'}
        elif action == 'peek':
            result = {'ok': True} if args['ticket'] in self.joined else {'ok': False, 'code': 'queue_expired'}
        else:
            self.joined.discard(args['ticket'])
            result = {'ok': True}

        class Reply:
            status = 200

            async def text(self):
                return json.dumps(result)
        return Reply()


async def test_binding_uses_one_ticket_while_waiting_and_new_ticket_after_upstream_attempt():
    binding = Binding()
    capacity = Capacity(binding)
    routes = [{'provider': 'openrouter', 'model': 'test/a:free', 'tokens': 100}]
    await capacity.start(routes)
    first = capacity.ticket
    await capacity.waiting()
    await capacity.waiting()
    assert capacity.ticket == first
    await capacity.reserve('openrouter', 'test/a:free', 100)
    assert capacity.ticket is None and not binding.joined
    await capacity.reserve('openrouter', 'test/a:free', 100)
    joins = [args['ticket'] for action, args in binding.calls if action == 'join']
    assert len(joins) == 2 and joins[0] != joins[1]
    await capacity.waiting()
    await capacity.close()
    assert not binding.joined


async def test_full_queue_is_bounded_refusal_and_no_reservation_is_sent():
    binding = Binding(); binding.full = True
    capacity = Capacity(binding)
    with pytest.raises(Failure) as caught:
        await capacity.start([{'provider': 'openrouter', 'model': 'test/a:free', 'tokens': 100}])
    assert caught.value.code == 'free_capacity_exhausted'
    assert caught.value.retry_after_seconds == 15
    assert 'queue is full' in caught.value.message
    assert not any(action == 'reserve' for action, _ in binding.calls)


async def test_fresh_token_requirements_update_the_existing_place_in_line():
    binding = Binding(); capacity = Capacity(binding)
    await capacity.start([{'provider': 'openrouter', 'model': 'test/a:free', 'tokens': 8000}])
    ticket = capacity.ticket
    await capacity.reserve('openrouter', 'test/a:free', 6000)
    joins = [args for action, args in binding.calls if action == 'join']
    assert [args['ticket'] for args in joins] == [ticket, ticket]
    assert joins[-1]['routes'][0]['tokens'] == 6000
