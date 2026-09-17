import { test } from 'node:test';
import assert from 'node:assert/strict';
import { handle, freePricing, freeModel, usableEndpoint, zero, validateInput } from '../src/index.js';

const model = (id = 'test/coder:free') => ({
  id, name: id, context_length: 128000, description: 'An agentic coding model.',
  pricing: { prompt: '0', completion: '0' }, supported_parameters: ['tools', 'max_tokens'],
});
const endpoint = id => ({
  model_id: id, status: 0, tag: 'test-provider/fp16', context_length: 128000,
  pricing: { prompt: '0', completion: '0', discount: 0 },
  supported_parameters: ['tools', 'max_tokens'], max_completion_tokens: 8192,
  uptime_last_30m: 99, uptime_last_5m: 100,
});
const answer = () => ({ choices: [{ finish_reason: 'stop', message: { role: 'assistant', content: 'done' } }], usage: { cost: 0 } });
const env = { OPENROUTER_API_KEY: 'test-secret-not-real' };
const input = (selected = 'auto') => ({ model: selected, messages: [{ role: 'user', content: 'hello' }] });
const request = body => new Request('https://bailout.test/v1/chat', { method: 'POST', body: JSON.stringify(body) });

function upstream({ catalog = [model()], mutateEndpoint, completion = answer(), status = 200, failMetadata = false } = {}) {
  const calls = [];
  const fetcher = async (url, init) => {
    calls.push({ url, init });
    if (url.endsWith('/models')) return Response.json({ data: catalog }, { status: failMetadata ? 503 : 200 });
    if (url.endsWith('/endpoints')) {
      const id = url.split('/models/')[1].replace('/endpoints', '');
      const e = endpoint(id);
      mutateEndpoint?.(e);
      return Response.json({ data: { id, endpoints: [e] } });
    }
    return Response.json(typeof completion === 'function' ? completion(calls) : completion, { status });
  };
  return { calls, fetcher, inferences: () => calls.filter(c => c.url.endsWith('/chat/completions')) };
}

test('strict exact-zero checks reject paid, unknown and underflow prices', () => {
  for (const bad of ['0.0000001', '1e-999', '', ' ', null, undefined, false, -1, 'NaN', 'free']) assert.equal(zero(bad), false, String(bad));
  for (const good of ['0', '0.000', '0e-100', 0]) assert.equal(zero(good), true);
  assert.equal(freePricing({ prompt: '0', completion: '0' }), true);
  for (const pricing of [{ prompt: '0' }, { prompt: '0', completion: '0', request: '0.01' }, { prompt: '0', completion: '0', future_fee: null }, { prompt: 0, completion: 0 }]) assert.equal(freePricing(pricing), false);
});

test('only explicit free variants with tools and usable endpoints qualify', () => {
  for (const id of ['openrouter/auto', 'openrouter/free', 'test/paid', 'test/coder:free:nitro', 'test/coder:free/../paid']) assert.equal(freeModel(model(id)), false);
  assert.equal(usableEndpoint(endpoint('test/coder:free'), 'test/coder:free'), true);
  for (const patch of [{ status: 1 }, { uptime_last_30m: null }, { uptime_last_5m: 0 }, { tag: '' }, { supported_parameters: [] }, { model_id: 'other/model:free' }]) assert.equal(usableEndpoint({ ...endpoint('test/coder:free'), ...patch }, 'test/coder:free'), false);
});

test('every inference gets fresh metadata, exact endpoints and a zero-price cap', async () => {
  const stub = upstream();
  for (let i = 0; i < 2; i++) assert.equal((await handle(request(input()), env, stub.fetcher)).status, 200);
  assert.equal(stub.calls.filter(c => c.url.endsWith('/models')).length, 2);
  assert.equal(stub.calls.filter(c => c.url.endsWith('/endpoints')).length, 2);
  for (const c of stub.inferences()) {
    const body = JSON.parse(c.init.body);
    assert.deepEqual(body.provider.max_price, { prompt: 0, completion: 0, request: 0, image: 0 });
    assert.deepEqual(body.provider.only, ['test-provider/fp16']);
    assert.equal(body.provider.allow_fallbacks, false);
    assert.equal(body.provider.require_parameters, true);
    assert.deepEqual(body.tools.map(t => t.function.name), ['bash']);
    assert.equal(c.init.headers.Authorization, 'Bearer test-secret-not-real');
    assert.equal(body.models, undefined);
    assert.equal(body.plugins, undefined);
  }
});

test('a model becoming paid between turns is never called again', async () => {
  const m = model();
  const stub = upstream({ catalog: [m] });
  assert.equal((await handle(request(input()), env, stub.fetcher)).status, 200);
  m.pricing.completion = '0.000001';
  assert.equal((await handle(request(input()), env, stub.fetcher)).status, 503);
  assert.equal(stub.inferences().length, 1);
});

test('paid endpoints, outages, and metadata errors fail closed', async () => {
  for (const options of [
    { mutateEndpoint: e => { e.pricing.request = '0.01'; } },
    { mutateEndpoint: e => { e.status = -1; } },
    { mutateEndpoint: e => { e.uptime_last_30m = 50; } },
    { failMetadata: true },
  ]) {
    const stub = upstream(options);
    assert.equal((await handle(request(input()), env, stub.fetcher)).status, 503);
    assert.equal(stub.inferences().length, 0);
  }
});

test('routing overrides, plugins and multimodal requests are rejected', async () => {
  const stub = upstream();
  for (const extra of [{ provider: { max_price: { prompt: 100 } } }, { plugins: [{ id: 'web' }] }, { models: ['paid/model'] }, { model: 'paid/model' }, { messages: [{ role: 'user', content: [{ type: 'image_url', image_url: { url: 'https://example.com/x' } }] }] }]) {
    assert.equal((await handle(request({ ...input(), ...extra }), env, stub.fetcher)).status, 400);
  }
  assert.equal(stub.calls.length, 0);
});

test('only Bash and complete tool histories are allowed', () => {
  const msg = { role: 'assistant', content: null, tool_calls: [{ id: 'a', type: 'function', function: { name: 'bash', arguments: '{"command":"pwd"}' } }] };
  const history = { messages: [msg, { role: 'tool', content: 'ok', tool_call_id: 'a' }] };
  assert.doesNotThrow(() => validateInput(history));
  assert.throws(() => validateInput({ messages: [msg] }));
  msg.tool_calls[0].function.name = 'read_file';
  assert.throws(() => validateInput(history));
});

test('fallback tries only separately verified free models, selection stays pinned', async () => {
  const catalog = [model('test/a:free'), model('test/b:free'), model('test/paid')];
  let count = 0;
  const stub = upstream({ catalog });
  const fetcher = (url, init) => {
    if (url.endsWith('/chat/completions') && count++ === 0) {
      stub.calls.push({ url, init });
      return Promise.resolve(Response.json({ error: { code: 429 } }, { status: 429 }));
    }
    return stub.fetcher(url, init);
  };
  assert.equal((await handle(request(input()), env, fetcher)).status, 200);
  assert.deepEqual(stub.inferences().map(c => JSON.parse(c.init.body).model), ['test/a:free', 'test/b:free']);
  const pinned = upstream({ catalog, status: 429 });
  assert.equal((await handle(request(input('test/a:free')), env, pinned.fetcher)).status, 429);
  assert.equal(pinned.inferences().length, 1);
});

test('unexpected cost and malformed/truncated tools are rejected before execution', async () => {
  for (const completion of [
    { ...answer(), usage: { cost: 0.1 } },
    { choices: [{ finish_reason: 'length', message: { role: 'assistant', content: 'partial' } }] },
    { choices: [{ message: { role: 'assistant', tool_calls: [{ id: 'a', type: 'function', function: { name: 'bash', arguments: '{' } }] } }] },
    { choices: [{ message: { role: 'assistant', content: null } }] },
  ]) {
    const stub = upstream({ completion });
    assert.equal((await handle(request(input()), env, stub.fetcher)).status, 502);
  }
});

test('rate limiting and size limits prevent upstream requests', async () => {
  const stub = upstream();
  const limited = { ...env, IP_LIMIT: { limit: async () => ({ success: false }) } };
  assert.equal((await handle(request(input()), limited, stub.fetcher)).status, 429);
  assert.equal((await handle(request({ messages: [{ role: 'user', content: 'x'.repeat(513000) }] }), env, stub.fetcher)).status, 413);
  assert.equal(stub.calls.length, 0);
});

test('model list exposes only free models and labels outages', async () => {
  const stub = upstream({ catalog: [model(), model('test/paid')], mutateEndpoint: e => { e.status = 1; } });
  const result = await handle(new Request('https://bailout.test/v1/models'), env, stub.fetcher);
  const body = await result.json();
  assert.equal(body.models.length, 1);
  assert.equal(body.models[0].available, false);
  assert.equal(body.default, null);
});

test('upstream error details never expose the server key', async () => {
  const stub = upstream({ status: 401, completion: { error: { message: env.OPENROUTER_API_KEY } } });
  const response = await handle(request(input()), env, stub.fetcher);
  assert.equal(response.status, 503);
  assert.equal((await response.text()).includes(env.OPENROUTER_API_KEY), false);
});
