import { test } from 'node:test';
import assert from 'node:assert/strict';
import worker from '../src/index.ts';

test('old API URLs preserve requests through the FastAPI service binding', async () => {
  const env = { API: { async fetch(request) {
    assert.equal(request.url, 'https://api.bailout.dev/v1/chat');
    assert.equal(request.method, 'POST');
    assert.equal(request.headers.get('CF-Connecting-IP'), '192.0.2.1');
    assert.deepEqual(await request.json(), { messages: [] });
    return Response.json({ ok: true });
  } } };
  const response = await worker.fetch(new Request('https://bailout.test/v1/chat', {method:'POST',headers:{'CF-Connecting-IP':'192.0.2.1'},body:JSON.stringify({messages:[]})}), env);
  assert.deepEqual(await response.json(), { ok: true });
});
test('installer redirects to the reviewable public source', async () => {
  const response = await worker.fetch(new Request('https://bailout.test/install.sh'), {});
  assert.equal(response.status, 302);
  assert.equal(response.headers.get('Location'), 'https://raw.githubusercontent.com/storozhenko98/bailout/main/install.sh');
});
test('website and docs use the asset binding', async () => {
  for (const path of ['/', '/docs/', '/style.css', '/missing']) {
    const response = await worker.fetch(new Request('https://bailout.test'+path), {ASSETS:{fetch:r=>new Response(new URL(r.url).pathname)}});
    assert.equal(await response.text(), path);
  }
});
