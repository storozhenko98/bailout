import json
from copy import deepcopy
from datetime import datetime, timezone, timedelta

import pytest
import httpx
from types import SimpleNamespace

from conftest import free_accounts, qualification
from context import budget, input_bound, quota_tokens
from providers import Providers, fingerprint, account_verified, zai_free_rows
from ranking import rank, qualified
from router import Router, BASH_TOOL, Failure, upstream_failure
from test_router import Upstream, Response, completion, data, model, endpoint, tool
from test_recovery import Sequence, stream, stream_answer
from test_capacity import Meter


async def qualify(route, models):
    rows = [qualification(m) for m in models]
    async def rankings():
        return {"models": rows, "health": {}}
    route.capacity.rankings = rankings


async def test_proactive_switch_preserves_history_and_announces_before_inference():
    a, b = model("test/a:free"), model("test/b:free")
    a["context_length"], b["context_length"] = 32768, 128000
    stub = Upstream(catalog=[a, b])
    route = Router(stub, "test")
    await qualify(route, await route.catalog())
    request = data()
    request["preferred_model"] = a["id"]
    request["messages"] += [dict(role="assistant", tool_calls=[tool()]), dict(role="tool", tool_call_id="call_a", content="x" * 28000)]
    before = deepcopy(request["messages"])
    result = await route.chat(request)
    assert result["model"] == b["id"]
    assert len(stub.inferences()) == 1
    assert stub.inferences()[0]["messages"] == before == request["messages"]
    assert "context limit" in result["notices"][0] and "preserved" in result["notices"][0]
    assert result["context"]["context_window"] == 128000


def test_endpoint_fit_includes_tool_schema_answer_and_margin():
    m = model(); m["context_length"] = 128000
    short, long = endpoint(), endpoint()
    short.update(context_length=32768, tag="short"); long.update(tag="long")
    messages = [dict(role="user", content="你🙂" * 5000)]
    result = budget(m, [short, long], messages, [BASH_TOOL])
    assert [e["tag"] for e in result["endpoints"]] == ["long"]
    assert result["input_tokens_upper_bound"] >= len(messages[0]["content"].encode())
    assert result["output_tokens"] == 4096 and result["margin_tokens"] == 12800
    short["max_prompt_tokens"] = 100
    assert budget(m, [short], [dict(role="user", content="hello")], [BASH_TOOL]) is None
    # Null output metadata means an unspecified cap; the request still imposes
    # its own bounded output and reserves that space in the context window.
    long['max_completion_tokens'] = None
    assert budget(m, [long], messages, [BASH_TOOL])['output_tokens'] == 4096


async def test_context_exhaustion_never_reserves_or_sends_or_drops_history():
    stub, meter = Upstream(), Meter()
    request = data(); request["messages"][0]["content"] = "x" * 150000
    with pytest.raises(Failure) as caught:
        await Router(stub, "test", capacity=meter).chat(request)
    assert caught.value.code == "context_exhausted"
    assert not meter.calls and not stub.inferences()
    assert len(request["messages"][0]["content"]) == 150000


def test_quota_estimate_does_not_treat_every_code_byte_as_a_token():
    messages = [dict(role="user", content="print('ready')\n" * 400)]
    # A modest coding conversation must not become permanently unroutable at
    # Groq's operator ceiling merely because the context check uses byte bounds.
    assert input_bound(messages, [BASH_TOOL]) + 2048 > 7800
    assert 2048 < quota_tokens(messages, [BASH_TOOL], 2048) < 7800
    # The separate estimate must never loosen the context fit check.
    large = [dict(role="user", content="你🙂" * 5000)]
    assert budget({"context_length": 32768}, [], large, [BASH_TOOL]) is None


async def test_provider_context_error_switches_without_poisoning_model_health():
    too_big = Response({"error": {"code": "context_length_exceeded"}}, 400)
    stub, meter = Sequence([too_big, Response(completion())]), Meter()
    result = await Router(stub, "test", capacity=meter).chat(data())
    assert result["model"] == "test/b:free"
    assert not meter.cooldowns and not result["failed_models"]
    assert "context limit" in result["notices"][0]


async def test_unknown_or_changed_models_are_not_sent_to_users():
    stub = Upstream(catalog=[model("brand/new:free")])
    with pytest.raises(Failure) as caught:
        await Router(stub, "test").chat(data())
    assert caught.value.code == "no_free_models" and not stub.inferences()
    stub = Upstream(); stub.catalog[0]["created"] = 999
    with pytest.raises(Failure):
        await Router(stub, "test").chat(data())
    assert not stub.inferences()


def test_quality_floor_is_separate_from_health_and_context():
    m = {"id": "provider/model:free", "context_length": 128000}; m["fingerprint"] = fingerprint(m)
    row = qualification(m)
    assert qualified(row, m)
    assert qualified({**row, "trials": 10, "passed": 8, "runs": 1}, m)
    assert qualified({**row, "trials": 20, "passed": 16, "runs": 2}, m)
    for changes in [dict(trials=9, passed=9, runs=1), dict(runs=0), dict(trials=10, passed=7, runs=1), dict(passed=15), dict(critical_failures=1), dict(native_tools=False), dict(fingerprint="0" * 64), dict(evaluated_at=(datetime.now(timezone.utc) - timedelta(days=31)).isoformat())]:
        assert not qualified({**row, **changes}, m)
    assert rank([m], {"models": [{**row, "passed": 1}], "health": {m["id"]: {"success_ewma": 1}}}) == []


def test_every_qualified_model_remains_available_and_health_can_outweigh_quality():
    a, b, c = [model(f'test/{name}:free') for name in ('strong', 'basic', 'poor')]
    for m in (a, b, c):
        m['fingerprint'] = fingerprint(m)
    snapshot = {'models': [qualification(m, trials=10, passed=passed, runs=1) for m, passed in [(a, 9), (b, 8), (c, 7)]]}
    assert [m['id'] for m in rank([a, b, c], snapshot)] == [a['id'], b['id']]
    snapshot['health'] = {a['id']: {'success_ewma': .4, 'updated': datetime.now(timezone.utc).timestamp() * 1000}}
    assert [m['id'] for m in rank([a, b, c], snapshot, preferred=a['id'])] == [b['id'], a['id']]
    snapshot['health'][a['id']]['updated'] -= 3600000
    assert rank([a, b, c], snapshot)[0]['id'] == a['id'], 'old failures must not permanently ban a route'


def test_observed_setup_regression_cannot_reenter_through_alias_or_new_nightly_score():
    for id in ('mistral/ministral-8b-2512:free', 'mistral/ministral-8b-latest:free', 'mistral/another-alias:free'):
        m = {**model(id), 'source': 'mistral', 'name': 'ministral-8b-2512'}
        m['fingerprint'] = fingerprint(m)
        row = qualification(m, passed=20)
        assert not qualified(row, m)
        assert rank([m], {'models': [row]}, preferred=id) == []


def test_more_evidence_breaks_close_scores_without_excluding_a_new_route():
    established, newcomer = model('test/established:free'), model('test/new:free')
    for m in (established, newcomer):
        m['fingerprint'] = fingerprint(m)
    snapshot = {'models': [qualification(established, passed=18), qualification(newcomer, trials=10, passed=9, runs=1)],
                'health': {established['id']: {'success_ewma': .95, 'updated': datetime.now(timezone.utc).timestamp() * 1000}}}
    assert [m['id'] for m in rank([newcomer, established], snapshot)] == [established['id'], newcomer['id']]
    snapshot['health'][established['id']]['success_ewma'] = .5
    assert rank([established, newcomer], snapshot)[0]['id'] == newcomer['id'], 'availability must still outweigh the evidence bonus'


class Direct:
    def __init__(self, provider, rows):
        self.provider, self.rows = provider, rows
        self.calls = []
    async def request(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url.endswith("/models"):
            return Response({"data": self.rows})
        return Response(completion())


def vercel_model():
    return {"id": "vendor/coder", "context_window": 128000, "max_tokens": 8192,
            "pricing": {"input": "0", "output": "0"}, "tags": ["free"], "supported_parameters": ["tools"]}


async def test_vercel_checks_live_prices_and_enforces_free_provider_filter():
    transport = Direct("vercel", [vercel_model()])
    route = Router(transport, "", provider_keys={"vercel": "fake-vercel"})
    await qualify(route, await route.providers.discover())
    result = await route.chat(data())
    body = json.loads(transport.calls[-1][1]["body"])
    assert body["providerOptions"]["gateway"] == {"has": ["free"], "models": ["vendor/coder"]}
    assert result["model"] == "vercel/vendor/coder:free" and result["free_only"] is True
    assert len([u for u, _ in transport.calls if u.endswith("/models")]) == 3
    transport.rows[0]["pricing"]["input"] = "0.000001"
    with pytest.raises(Failure):
        await route.chat(data())
    assert len([u for u, _ in transport.calls if u.endswith("/chat/completions")]) == 1


async def test_vercel_unknown_additional_fees_and_paid_credit_models_are_excluded():
    for patch in [{"pricing": {"input": "0", "output": "1"}}, {"pricing": {"input": "0", "output": "0", "request": ".01"}}, {"pricing": {"input": "0", "output": "0", "unknown_fee": None}}, {"tags": []}, {"supported_parameters": []}]:
        p = Providers(Direct("vercel", [{**vercel_model(), **patch}]), {"vercel": "fake"})
        assert await p.discover() == []


async def test_groq_sequential_tool_configuration_is_fingerprinted_and_sent():
    transport = Direct('groq', [{'id': 'openai/gpt-oss-120b', 'context_window': 131072}])
    p = Providers(transport, {'groq': 'fake'}, free_accounts())
    model = (await p.discover())[0]
    assert model['parameters'] == {'reasoning_effort': 'low', 'parallel_tool_calls': False}
    body = p.body(model, [{'role': 'user', 'content': 'repair a config'}], True, 2048)
    assert body['parallel_tool_calls'] is False
    assert body['tools'][0]['function']['name'] == 'bash'
    from providers import fingerprint
    old = {**model, 'parameters': {'reasoning_effort': 'low'}}
    assert fingerprint(old) != model['fingerprint']


async def test_mistral_free_attestation_tools_context_and_call_id_mapping():
    transport = Direct("mistral", [{"id": "devstral-2507", "max_context_length": 128000,
        "capabilities": {"function_calling": True, "completion_chat": True}}])
    p = Providers(transport, {"mistral": "fake"})
    assert await p.discover() == [] and not transport.calls
    p.accounts = free_accounts()
    m = (await p.discover())[0]
    transport.rows[0]['created'] = 999
    assert (await p.discover())[0]['fingerprint'] == m['fingerprint']
    messages = [dict(role="assistant", tool_calls=[tool()], reasoning_details=[]), dict(role="tool", tool_call_id="call_a", content="done")]
    before = deepcopy(messages)
    body = p.body(m, messages, False, 4096)
    id = body["messages"][0]["tool_calls"][0]["id"]
    assert len(id) == 9 and id.isalnum() and body["messages"][1]["tool_call_id"] == id
    assert messages == before and "reasoning_details" not in body["messages"][0]
    p.accounts["mistral"]["billing_disabled"] = False
    assert await p.discover() == []


def test_account_attestations_expire_and_never_enable_unknown_billing():
    policy = free_accounts()["groq"]
    assert account_verified(policy)
    for changes in [dict(tier="paid"), dict(billing_disabled=False), dict(topups_disabled=False), dict(expires_at="bad"), dict(expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())]:
        assert not account_verified({**policy, **changes})


def test_zai_only_exact_free_inference_rows_qualify():
    text = "| GLM-4.5-Flash | Free | Free | Free | Free |\n| GLM-4.7-Flash | $1 | Free | Free | $2 |"
    assert zai_free_rows(text) == {"glm-4.5-flash"}
    assert not zai_free_rows(text.replace("Free", "Limited-time Free"))


@pytest.mark.parametrize('patch', [dict(context_length=None), dict(context_length='128000'), dict(max_completion_tokens='8192'), dict(max_prompt_tokens=-1)])
def test_malformed_endpoint_budgets_cannot_admit_inference(patch):
    assert budget(model(), [{**endpoint(), **patch}], [dict(role='user', content='hello')], [BASH_TOOL]) is None


async def test_streamed_context_switch_precedes_inference_and_keeps_large_history():
    a, b = model('test/a:free'), model('test/b:free')
    a['context_length'], b['context_length'] = 32768, 256000
    stub = Upstream(catalog=[a, b], patch={'context_length': 256000})
    route = Router(stub, 'test')
    await qualify(route, await route.catalog())
    request = data(stream=True)
    request['preferred_model'] = a['id']
    request['messages'][0]['content'] = 'x' * 105000
    candidates = await route.prepare(request)
    events = route.completion_events(request, candidates)
    notice = await anext(events)
    assert notice['reason'] == 'context_limit' and notice['model'] == b['id']
    assert notice['retry'] is True and not stub.inferences()
    await events.aclose()
    assert len(request['messages'][0]['content']) == 105000


async def test_evaluation_bypasses_only_quality_and_requires_private_binding(monkeypatch):
    import app as application
    stub = Upstream(catalog=[model('test/unqualified:free')])
    monkeypatch.setattr(application, 'router', lambda request: Router(stub, 'test'))
    candidate = (await Router(stub, 'test').catalog())[0]
    body = {**data(), 'candidate': candidate['id'], 'fingerprint': candidate['fingerprint']}
    headers = {'X-Bailout-Evaluation': 'authorized'}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application.app), base_url='https://test') as client:
        assert (await client.post('/internal/bench/chat', headers=headers, json=body)).status_code == 404
        assert (await client.post('/v1/chat', headers=headers, json=data())).status_code == 503
    async def private(scope, receive, send):
        scope['env'] = SimpleNamespace()
        await application.app(scope, receive, send)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=private), base_url='https://private') as client:
        assert (await client.post('/internal/bench/chat', json=body)).status_code == 404
        assert (await client.post('/internal/bench/chat', headers=headers, json={**body, 'fingerprint': 'bad'})).status_code == 503
        result = await client.post('/internal/bench/chat', headers=headers, json=body)
        assert result.status_code == 200 and result.json()['model'] == candidate['id']
        stub.catalog[0]['pricing']['prompt'] = '.01'
        assert (await client.post('/internal/bench/chat', headers=headers, json=body)).status_code == 503
    assert len(stub.inferences()) == 1


async def test_vercel_prices_rechecked_before_retry():
    class Changed(Direct):
        async def request(self, url, **kwargs):
            if url.endswith('/chat/completions'):
                self.calls.append((url, kwargs))
                self.rows[0]['pricing']['output'] = '.01'
                return Response({'error': {'code': 429}}, 429)
            return await super().request(url, **kwargs)
    async def no_wait(seconds):
        pass
    transport = Changed('vercel', [vercel_model()])
    route = Router(transport, '', provider_keys={'vercel': 'fake'}, sleep=no_wait)
    await qualify(route, await route.providers.discover())
    with pytest.raises(Failure):
        await route.chat(data())
    assert len([u for u, _ in transport.calls if u.endswith('/chat/completions')]) == 1


@pytest.mark.parametrize('status', [429, 503])
async def test_evaluation_exposes_short_retry_without_switching_models(status):
    transport = Sequence([Response({'error': {'code': status}}, status)])
    meter = Meter()
    route = Router(transport, 'test', capacity=meter)
    route.evaluation = True
    request = data(); request['model'] = 'test/a:free'
    with pytest.raises(Failure) as caught:
        await route.chat(request)
    assert caught.value.retry_after_seconds == 2
    assert len(transport.inferences()) == 1
    assert len(meter.cooldowns) == 1 and meter.cooldowns[0][-1] == 2


async def test_zai_live_docs_and_prices_gate_every_attempt():
    from providers import ZAI_PRICING, ZAI_DOCS
    class Zai:
        paid = False
        calls = 0
        async def request(self, url, **kwargs):
            if url == ZAI_PRICING:
                return Response({}, raw=b'| GLM-4.5-Flash | Free | Free | Free | Free |' if not self.paid else b'| GLM-4.5-Flash | $1 | Free | Free | $1 |')
            if url == ZAI_DOCS['glm-4.5-flash']:
                return Response({}, raw=b'GLM-4.5-Flash Function Call <Card title="Context Length">128K</Card> <Card title="Maximum Output Tokens">96K</Card>')
            self.calls += 1
            body = json.loads(kwargs['body'])
            assert body['model'] == 'glm-4.5-flash' and body['thinking'] == {'type': 'disabled'}
            assert body['tools'][0]['function']['name'] == 'bash'
            return Response(completion())
    transport = Zai()
    route = Router(transport, '', provider_keys={'zai': 'fake'})
    await qualify(route, await route.discover())
    assert (await route.chat(data()))['provider'] == 'zai'
    transport.paid = True
    with pytest.raises(Failure):
        await route.chat(data())
    assert transport.calls == 1
