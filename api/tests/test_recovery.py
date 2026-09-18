import asyncio
import json
from copy import deepcopy

import httpx
import pytest

import router as policy
from app import app
from router import Failure, Router, validate
from test_router import Response, Upstream, completion, data, model, sse, tool


@pytest.mark.parametrize('business,code,delay', [
    ('1113', 'upstream_quota', 3600), ('1308', 'upstream_quota', 3600),
    ('1310', 'upstream_quota', 3600), ('1311', 'provider_unavailable', 3600),
    ('1302', 'provider_rate_limited', 10), ('1305', 'provider_rate_limited', 10),
    ('1313', 'upstream_policy', None),
])
@pytest.mark.parametrize('numeric', [False, True])
def test_zai_business_codes_distinguish_quota_and_overload_without_echoing_bodies(business, code, delay, numeric):
    failure = policy.upstream_failure(429, {'error': {'code': int(business) if numeric else business,
        'message': 'private echoed prompt'}}, 'zai')
    assert failure.code == code
    assert failure.retry_after_seconds == delay
    assert failure.provider_error_code == business
    assert failure.recoverable is (code != 'upstream_policy')
    assert 'private echoed prompt' not in json.dumps(failure.payload())


def stream_answer(text="Recovered"):
    return Response(None, raw=sse(dict(choices=[dict(delta=dict(content=text), finish_reason="stop")], usage=dict(cost=0))))


async def test_zai_streamed_business_error_keeps_its_meaning():
    response = Response(None, raw=sse(dict(error=dict(code='1113', message='private prompt'))))
    with pytest.raises(Failure) as caught:
        async for _ in Router(Upstream(), 'test').read_stream('zai/glm-4.7-flash:free', response, 'zai'):
            pass
    assert caught.value.code == 'upstream_quota'
    assert caught.value.provider_error_code == '1113'


def error(status, **metadata):
    return Response(dict(error=dict(code=status, message="Upstream error", metadata=metadata)), status)


class Sequence(Upstream):
    def __init__(self, responses):
        super().__init__(catalog=[model(f"test/{c}:free") for c in "abcd"])
        self.sequence = list(responses)

    async def request(self, url, **kwargs):
        response = await super().request(url, **kwargs)
        if not url.endswith("/chat/completions"):
            return response
        await response.close()
        selected = self.sequence.pop(0)
        if isinstance(selected, BaseException):
            raise selected
        self.responses.append(selected)
        return selected


async def stream(route, request):
    candidates = await route.prepare(request)
    return [json.loads(e) async for e in route.stream_chat(request, candidates)]


@pytest.mark.parametrize("failure", [
    lambda: error(503),
    lambda: TimeoutError(), lambda: ConnectionError(),
    lambda: Response(None, raw=b"data: broken\n\n"),
    lambda: Response(None, raw=sse(dict(choices=[dict(delta=dict(content="discard this"))]), done=False)),
    lambda: Response(None, raw=sse(dict(choices=[dict(delta={}, finish_reason="stop")]))),
    lambda: Response(None, raw=sse(dict(error=dict(code=502, metadata=dict(error_type="provider_unavailable"))))),
    lambda: Response(None, raw=sse(dict(choices=[dict(delta=dict(tool_calls=[dict(index=0, id="bad", type="function", function=dict(name="bash", arguments="{"))]), finish_reason="tool_calls")]))),
])
async def test_auto_recovers_with_one_valid_final_response(failure):
    stub = Sequence([failure(), stream_answer()])
    events = await stream(Router(stub, "test"), data(stream=True))
    assert [b["model"] for b in stub.inferences()] == ["test/a:free", "test/b:free"]
    assert len([e for e in events if e["type"] == "done"]) == 1
    assert events[-1]["message"]["content"] == "Recovered"
    assert events[-1]["failed_models"] == ["test/a:free"]
    assert any(e.get("retry") for e in events)
    assert all(r.closed for r in stub.responses)


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("status,metadata,code", [
    (401, {}, "upstream_authentication"), (402, {}, "upstream_quota"),
    (403, {}, "upstream_policy"),
    (429, dict(provider_name="provider", limit_source="openrouter_daily"), "upstream_quota"),
    (400, {}, "invalid_model_request"),
])
async def test_global_refusals_never_fall_back(streaming, status, metadata, code):
    stub = Sequence([error(status, **metadata), stream_answer()])
    route = Router(stub, "test")
    if streaming:
        events = await stream(route, data(stream=True))
        assert events[-1]["type"] == "error" and events[-1]["code"] == code
        assert not any(e.get("retry") for e in events)
    else:
        with pytest.raises(Failure) as caught:
            await route.chat(data())
        assert caught.value.code == code
    assert len(stub.inferences()) == 1


@pytest.mark.parametrize("status", [401, 402, 403])
async def test_in_stream_global_refusal_is_also_terminal(status):
    stub = Sequence([Response(None, raw=sse(dict(error=dict(code=status)))), stream_answer()])
    events = await stream(Router(stub, "test"), data(stream=True))
    assert events[-1]["type"] == "error"
    assert len(stub.inferences()) == 1


async def test_cost_audit_stops_even_if_stream_then_breaks():
    stub = Sequence([Response(None, raw=sse(dict(choices=[], usage=dict(cost=.01)), done=False)), stream_answer()])
    events = await stream(Router(stub, "test"), data(stream=True))
    assert events[-1]["code"] == "unexpected_cost"
    assert len(stub.inferences()) == 1


@pytest.mark.parametrize("failure", [lambda: error(503), lambda: TimeoutError()])
async def test_pinned_never_switches(failure):
    stub = Sequence([failure(), stream_answer()])
    events = await stream(Router(stub, "test"), data("test/a:free", stream=True))
    assert events[-1]["type"] == "error"
    assert "selected model is unchanged" in events[-1]["error"]
    assert len(stub.inferences()) == 1


async def test_attempt_ceiling_and_failed_models_on_exhaustion():
    stub = Sequence([error(503) for _ in range(4)])
    events = await stream(Router(stub, "test"), data(stream=True))
    assert len(stub.inferences()) == 4
    assert events[-1]["code"] == "recovery_exhausted"
    assert events[-1]["failed_models"] == [f"test/{c}:free" for c in "abcd"]


async def test_nonstream_invalid_result_recovers_and_retains_history():
    stub = Sequence([Response(completion(dict(role="assistant", content=""))), Response(completion())])
    request = data()
    request["messages"] += [dict(role="assistant", tool_calls=[tool()]), dict(role="tool", tool_call_id="call_a", content="already ran")]
    before = deepcopy(request["messages"])
    result = await Router(stub, "test").chat(request)
    assert result["model"] == "test/b:free"
    assert all(b["messages"] == before for b in stub.inferences())
    assert request["messages"] == before


async def test_session_preferences_still_require_fresh_free_checks():
    stub = Sequence([Response(completion())])
    result = await Router(stub, "test").chat(validate({**data(), "preferred_model":"test/c:free", "avoid_models":["test/a:free"]}))
    assert result["model"] == "test/c:free"
    stub = Sequence([Response(completion())])
    stub.catalog[2]["pricing"]["prompt"] = "0.01"
    result = await Router(stub, "test").chat(validate({**data(), "preferred_model":"test/c:free", "avoid_models":["test/a:free"]}))
    assert result["model"] == "test/b:free"


async def test_price_changes_during_fallback_skip_paid_candidate():
    stub = Sequence([error(503), Response(completion())])
    original = stub.request
    async def request(url, **kwargs):
        result = await original(url, **kwargs)
        if url.endswith("/chat/completions"):
            stub.catalog[1]["pricing"]["prompt"] = "0.01"
        return result
    stub.request = request
    result = await Router(stub, "test").chat(data())
    assert result["model"] == "test/c:free"
    assert [b["model"] for b in stub.inferences()] == ["test/a:free", "test/c:free"]


async def test_expired_client_failure_hints_cannot_hide_every_free_route():
    from test_capacity import Meter
    stub = Sequence([Response(completion())])
    meter = Meter({'openrouter': dict(ok=False, code='provider_cooldown', scope='provider', retry_after_seconds=60)})
    request = validate({**data(), 'avoid_models': [f'test/{c}:free' for c in 'abcd']})
    with pytest.raises(Failure) as caught:
        await Router(stub, 'test', capacity=meter).chat(request)
    assert caught.value.code == 'free_capacity_exhausted' and not stub.inferences()
    meter.deny.clear()
    stub.catalog[0]['pricing']['prompt'] = '0.01'
    result = await Router(stub, 'test', capacity=meter).chat(request)
    assert result['model'] == 'test/b:free'
    assert all(b["provider"]["max_price"]["prompt"] == 0 and not b["provider"]["allow_fallbacks"] for b in stub.inferences())


async def test_metadata_failure_during_recovery_fails_closed():
    stub = Sequence([error(503), Response(completion())])
    original = stub.request
    async def request(url, **kwargs):
        if url.endswith("/models") and stub.inferences():
            return Response({}, 503)
        return await original(url, **kwargs)
    stub.request = request
    with pytest.raises(Failure) as caught:
        await Router(stub, "test").chat(data())
    assert caught.value.code == "pricing_unavailable"
    assert len(stub.inferences()) == 1


async def test_deadline_and_cancel_do_not_start_another_attempt(monkeypatch):
    monkeypatch.setattr(policy, "REQUEST_SECONDS", .03)
    class Slow(Response):
        async def chunks(self):
            await asyncio.sleep(10)
            yield b""
    response = Slow(None)
    stub = Sequence([response, stream_answer()])
    events = await stream(Router(stub, "test"), data(stream=True))
    assert events[-1]["code"] == "recovery_exhausted"
    assert len(stub.inferences()) == 1 and response.closed
    monkeypatch.setattr(policy, "REQUEST_SECONDS", 120)
    response = Slow(None)
    stub = Sequence([response, stream_answer()])
    task = asyncio.create_task(stream(Router(stub, "test"), data(stream=True)))
    await asyncio.sleep(.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(stub.inferences()) == 1 and response.closed


async def test_per_attempt_timeout_leaves_time_for_recovery(monkeypatch):
    monkeypatch.setattr(policy, "AUTO_ATTEMPT_SECONDS", .02)
    class Slow(Response):
        async def chunks(self):
            await asyncio.sleep(10)
            yield b""
    first = Slow(None)
    stub = Sequence([first, stream_answer()])
    events = await stream(Router(stub, "test"), data(stream=True))
    assert events[-1]["type"] == "done" and events[-1]["model"] == "test/b:free"
    assert first.closed


async def test_retry_after_is_preserved_for_provider_cooldown():
    first = error(429, provider_name="provider")
    first.retry_after = "600"
    stub = Sequence([first, Response(completion())])
    result = await Router(stub, "test").chat(data())
    assert result["cooldown_seconds"] == 600


async def test_single_auto_route_can_use_response_window_but_not_exceed_request_deadline(monkeypatch):
    monkeypatch.setattr(policy, "AUTO_ATTEMPT_SECONDS", .01)
    monkeypatch.setattr(policy, "PINNED_ATTEMPT_SECONDS", .2)
    class Delayed(Response):
        async def chunks(self):
            await asyncio.sleep(.04)
            yield sse(dict(choices=[dict(delta=dict(content="Ready"), finish_reason="stop")]))
    for deadline, expected in [(.5, "done"), (.02, "error")]:
        monkeypatch.setattr(policy, "REQUEST_SECONDS", deadline)
        response = Delayed(None)
        stub = Sequence([response])
        stub.catalog = [model("test/a:free")]
        events = await stream(Router(stub, "test"), data(stream=True))
        assert events[-1]["type"] == expected
        assert len(stub.inferences()) == 1 and response.closed
        if expected == "error":
            assert "did not respond in time" in events[-1]["error"]


async def test_last_fallback_gets_response_time_after_earlier_route_failed(monkeypatch):
    monkeypatch.setattr(policy, 'AUTO_ATTEMPT_SECONDS', .01)
    monkeypatch.setattr(policy, 'PINNED_ATTEMPT_SECONDS', .2)
    class Delayed(Response):
        async def chunks(self):
            await asyncio.sleep(.04)
            yield sse(dict(choices=[dict(delta=dict(content='Recovered'), finish_reason='stop')]))
    stub = Sequence([error(503), Delayed(None)])
    stub.catalog = [model('test/a:free'), model('test/b:free')]
    events = await stream(Router(stub, 'test'), data(stream=True))
    assert events[-1]['type'] == 'done' and events[-1]['model'] == 'test/b:free'


async def test_switch_strips_opaque_reasoning_but_keeps_completed_tools():
    stub = Sequence([error(503), Response(completion())])
    request = data()
    request["messages"] += [dict(role="assistant", tool_calls=[tool()], reasoning_details=[dict(data="model-a-signature")]),
                            dict(role="tool", tool_call_id="call_a", content="already ran")]
    await Router(stub, "test").chat(request)
    assert stub.inferences()[0]["messages"][1]["reasoning_details"]
    assert "reasoning_details" not in stub.inferences()[1]["messages"][1]
    assert stub.inferences()[1]["messages"][2]["content"] == "already ran"


@pytest.mark.parametrize("status", [401])
async def test_unreadable_global_error_body_never_becomes_a_retry(monkeypatch, status):
    monkeypatch.setattr(policy, "AUTO_ATTEMPT_SECONDS", .01)
    class Slow(Response):
        async def chunks(self):
            await asyncio.sleep(10)
            yield b""
    first = Slow(None, status)
    stub = Sequence([first, stream_answer()])
    events = await stream(Router(stub, "test"), data(stream=True))
    assert events[-1]["code"] in ("upstream_authentication", "upstream_rate_limited")
    assert len(stub.inferences()) == 1 and first.closed


def test_invalid_or_pinned_hints_rejected():
    for patch in [dict(preferred_model="paid/model"), dict(avoid_models="test/a:free"), dict(avoid_models=["paid/model"]),
                  dict(avoid_models=["test/a:free"] * 36), dict(model="test/a:free", preferred_model="test/b:free")]:
        with pytest.raises(Failure):
            validate({**data(), **patch})


async def test_fastapi_stream_protocol_recovers_without_error_event(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "placeholder")
    app.state.transport = Sequence([error(503), stream_answer()])
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/v1/chat", json=data(stream=True))
        assert response.status_code == 200
        events = [json.loads(line) for line in response.text.splitlines()]
        assert events[-1]["type"] == "done"
        assert any(e.get("retry") for e in events)
        assert not any(e["type"] == "error" for e in events)
    finally:
        app.state.transport = None
