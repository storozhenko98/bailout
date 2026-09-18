import { transformSync } from 'esbuild';
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { Miniflare, convertV4MiniflareOptions } from 'miniflare';

const source = transformSync(await readFile(new URL('../src/stats.ts', import.meta.url), 'utf8'), {loader:'ts',format:'esm'}).code;
const probe = `
  import { PublicStats } from './stats.js';
  export class StatsProbe {
    constructor(ctx) { this.stats = new PublicStats(ctx.storage, Date.now(), fetch, 'test-only-token'); }
    async fetch() { await this.stats.refresh(); return Response.json(this.stats.snapshot()); }
    async alarm() {}
  }
  export default { fetch(request, env) { return env.PROBE.getByName('test').fetch(request); } };
`;

// Node's fetch accepts options that workerd rejects. Exercise the production
// stats code with real Workers fetch and SQLite, but no external network or key.
for (const redirect of [false, true]) {
  test(`Workers download refresh ${redirect ? 'rejects redirects without forwarding credentials' : 'authenticates and persists the binary total'}`, async () => {
    const requests = [];
    const mf = new Miniflare(convertV4MiniflareOptions({
      compatibilityDate: '2026-09-17',
      modules: [
        { type: 'ESModule', path: 'probe.js', contents: probe },
        { type: 'ESModule', path: 'stats.js', contents: source },
      ],
      durableObjects: { PROBE: { className: 'StatsProbe', useSQLite: true } },
      outboundService: request => {
        requests.push({ url: request.url, authorization: request.headers.get('Authorization') });
        return redirect
          ? new Response(null, { status: 302, headers: { Location: 'https://unexpected.example/token-target' } })
          : Response.json([{ assets: [{ id: 1, name: 'bailout-macos-arm64.tar.gz', download_count: 19 }] }]);
      },
    }));
    try {
      const result = await (await mf.dispatchFetch('https://local.test/stats')).json();
      assert.equal(requests.length, 1, 'no redirect target is ever contacted');
      assert.equal(requests[0].url, 'https://api.github.com/repos/storozhenko98/bailout/releases?per_page=100&page=1');
      assert.equal(requests[0].authorization, 'Bearer test-only-token');
      assert.equal(result.downloads.total, redirect ? null : 19);
      assert.equal(result.downloads.last_error, redirect ? 'github_http_302' : null);
      assert.ok(!JSON.stringify(result).includes('test-only-token'));
    } finally { await mf.dispose(); }
  });
}
