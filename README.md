# bailout

A tiny terminal coding agent. **One tool: Bash. Full auto. Free models only.**

```sh
curl -fsSL https://bailout.bailout-router.workers.dev/install.sh | bash
bailout
```

Apple Silicon macOS, x64 Linux, and arm64 Linux. Requires the system's `bash` and
`curl`; no Node, Python, local model, or API key. The native Rust executable is
about **0.4 MB on Apple Silicon**. Release builds must stay below **6,000,000 bytes**
uncompressed. Linux releases use static musl, so there is no glibc version dependency.

```text
$ bailout

bailout · full auto · bash only · free models
/help for commands · Ctrl-C stops a task

› fix the failing test and run the suite
```

Commands run immediately with your account's permissions. This is not a sandbox.
Run it in a directory you trust. The model can read, edit, delete, execute programs,
and access the network through Bash. No command approval prompts or command allowlist.

## Use

```sh
bailout 'find and fix the bug in the parser'
printf 'explain this project' | bailout
bailout models
bailout --model poolside/laguna-s-2.1:free 'add a test for empty input'
bailout --max-steps 100 'finish the migration'
```

| In a session | Action |
| --- | --- |
| `/models` | List free models, coding preference order, and live provider health |
| `/model 2` | Pick a number from the last model list |
| `/model vendor/model:free` | Pin an explicit free model |
| `/model auto` | Pick a healthy free model automatically; the default |
| `/model` | Show the current selection |
| `/new` | Clear the conversation |
| `/help` | Show help |
| `/exit` | Quit |

Ctrl-C stops the current request or Bash process group, including pipelines and
children. Interactive sessions return to the prompt. One-shot cancellation exits 130.
Each tool call starts a fresh noninteractive shell in the session directory unless
the model supplies `workdir`. Shell variables and `cd` do not persist between calls.
Background servers should redirect their output. Default command timeout: 2 minutes;
the model can request up to 30 minutes. Each task allows 50 model steps by default.
Tool output is bounded; old complete conversation turns are dropped when needed.
Conversations stay in memory and are not saved to disk.
The first model step of each task must call Bash; a text-only claim that work was
done is rejected when no tool has run. Subsequent steps can finish with a text answer.

This intentionally uses ordinary terminal line input and scrollback, not a full-screen
TUI. There are no plugins, MCP, extra file tools, browser tools, or local inference.

## Free means zero

The hosted Cloudflare Worker owns the OpenRouter key. The client never receives it.
For **every model request**, including subsequent agent steps and fallback attempts:

1. Fetch a fresh OpenRouter catalog with cache bypass; require an explicit `:free`
   model, native tool support, and exactly zero in every reported pricing field.
2. Fetch that model's current endpoints. Require matching model IDs, zero endpoint
   prices, tool support, an operational status, and at least 90% reported uptime.
   Missing prices or unknown health fail closed.
3. Send only the exact verified provider endpoints with `allow_fallbacks: false`,
   `require_parameters: true`, and a hard zero `max_price` for prompt, completion,
   request, and image charges. This cap also covers the gap between checking and sending.
4. On provider errors, `auto` can try at most three independently checked free models.
   An explicitly selected model stays pinned. No paid model fallback exists.
5. Reject arbitrary routing overrides, provider keys, plugins, multimodal content,
   and tools other than Bash. Audit returned costs and reject unexpected nonzero costs.

The $1 key limit alone would still permit paid requests. The checks and upstream
zero-price cap are what enforce this app's free-only policy. Like any client, the
app depends on OpenRouter honoring its published prices and routing contract.

Model ordering is a **metadata heuristic, not measured benchmark scores**. It favors
coding specialists, reasoning support, coding evaluation mentions, and useful context
lengths; small variants and models described as unsuitable for coding are deprioritized.
Only models with healthy verified-free endpoints are eligible for automatic selection.
The list includes the score and reasons so the preference is inspectable and replaceable.

Free capacity is **shared and best effort**. OpenRouter's account quota and provider
availability still apply. HTTP 429 means wait or choose another available free model;
the app never pays to bypass it. Cloudflare rate limits reduce burst abuse but are
per-location, approximate limits, not a global quota or identity system.

Source contracts: [OpenRouter provider routing](https://openrouter.ai/docs/guides/routing/provider-selection),
[OpenRouter limits](https://openrouter.ai/docs/api/reference/limits),
[Cloudflare Workers pricing](https://developers.cloudflare.com/workers/platform/pricing/).

## Privacy

Prompts, model-selected file contents, and Bash output are sent through the hosted
Worker to OpenRouter and its selected inference provider. Provider data policies apply;
free does not mean zero data retention. The Worker does not store conversations or
log request bodies. Worker observability is disabled. Do not include credentials in
prompts or ask the agent to read secret files.

## Build and test

```sh
cargo test --locked
cargo clippy --locked --all-targets -- -D warnings
cargo build --release --locked
python3 scripts/check-size.py target/release/bailout
python3 scripts/smoke.py target/release/bailout
cd worker && npm ci && npm test
```

Only two direct Rust dependencies: `serde_json` and `libc`. System curl handles TLS;
Bash handles everything the model does. The Worker is about 5 KB gzipped with no
runtime dependencies. A native JavaScript Worker keeps this proxy smaller and simpler
than adding FastAPI, ASGI, and a Python runtime.

CI runs the tests and real binary smoke checks on all three supported platforms.
Tagging `vX.Y.Z` builds native release assets, tests them, enforces the size ceiling,
and publishes SHA-256 checksums. The installer pins one resolved release version,
verifies the archive checksum, checks its contents and binary size, then installs
atomically. Set `BAILOUT_VERSION=v0.1.0` or `BAILOUT_INSTALL_DIR=/your/bin` to override.
It prefers an existing writable PATH location and never uses sudo or modifies shell rc files.

## Host your own router

```sh
cd worker
npm ci
npx wrangler login
npx wrangler secret put OPENROUTER_API_KEY
npm run deploy
```

Enter the OpenRouter key at the secret prompt. Never put it in source or `wrangler.jsonc`.
For local development, place it in a gitignored `.dev.vars` file and run `npm run dev`.

```sh
export BAILOUT_API_URL=https://your-worker.your-subdomain.workers.dev
bailout
```

`BAILOUT_MODEL` sets the default model. `BAILOUT_DEFAULT_API` at compile time changes
the binary's built-in backend URL. Runtime API overrides require HTTPS, except localhost
for development. The public service intentionally requires no login, so use your own
Worker and key if you need a separate quota. Cloudflare hosting limits are separate
from model prices; no paid infrastructure subscription is required by this repository.

MIT licensed. Inspired by the small, shell-like feel of [fx](https://fx.sh/).
