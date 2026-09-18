import { cp, mkdir } from 'node:fs/promises';
import { build } from 'esbuild';

await mkdir('../dist/site', { recursive: true });
await cp('../site', '../dist/site', { recursive: true });
await build({ entryPoints: ['../web/app.ts', '../web/stats.ts'], outdir: '../dist/site',
  bundle: true, format: 'esm', target: 'es2022', minify: true });
