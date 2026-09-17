# bailout

A tiny terminal coding agent. **One tool: Bash. Full auto. Free models only.**

```sh
curl -fsSL https://bailout.bailout-router.workers.dev/install.sh | bash
bailout
```

Apple Silicon macOS, x64 Linux, and arm64 Linux. Requires the system's `bash` and
`curl`; no Node, Python, local model, or API key. The native Rust executable is
about **0.6 MB on Apple Silicon**. Release builds must stay below **6,000,000 bytes**
uncompressed. Linux releases use static musl, so there is no glibc version dependency.

```text
$ bailout

  bailout v0.2.0
  ~/my-project

  › auto · free models · full access
  /model choose a model   /help shortcuts

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
| `/model` | Open the interactive model picker |
| `/last` | Expand the last command’s captured output |
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
Normal conversation can return a direct answer. Bash is called when the model needs to
inspect, change, or verify something. There is no forced tool call. Replies stream as
they arrive; tool calls execute only after a complete, validated final response.

The terminal editor supports history, Unicode, bracketed paste, Ctrl-J / Alt-Enter
for multiline input, and Ctrl-A / Ctrl-E. Ctrl-C clears a draft or exits an empty
prompt. The interface uses ordinary terminal scrollback, so commands and answers
remain readable after a task finishes. There are no plugins, MCP, extra file tools, browser tools, or local inference.

## Free means zero

The hosted FastAPI service on Cloudflare Python Workers owns the OpenRouter key. The client never receives it.
For **every model request**, including subsequent agent steps and fallback attempts:

1. Fetch a fresh OpenRouter catalog with cache bypass; require an explicit `:free`
   model, native tool support, and exactly zero in every reported pricing field.
2. Fetch that model's current endpoints. Require matching model IDs, zero endpoint
   prices, tool support, an operational status, and at least 95% reported uptime over 30 minutes (and over 5 minutes when reported).
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
python3 scripts/test-installer.py
(cd api && uv sync && uv run pytest)
(cd worker && npm ci && npm test)
```

Optional live file-write verification (uses shared free quota):
`python3 scripts/live-smoke.py target/release/bailout`.

Three direct Rust dependencies: `serde_json`, `libc`, and `rustyline`. System curl
handles TLS; Bash handles everything the model does. The API is FastAPI on
Cloudflare Python Workers. A separate Worker serves the static landing page and
forwards legacy API URLs to FastAPI.

The smoke test drives a real controlling terminal. It verifies Ctrl-C as a keypress
while editing, waiting on a model, running a child process, and at the empty prompt,
plus history, multiline input, model selection, and recovery after interruption.

CI runs the tests and real binary smoke checks on all three supported platforms.
Tagging `vX.Y.Z` builds native release assets, tests them, enforces the size ceiling,
and publishes SHA-256 checksums. The installer pins one resolved release version,
verifies the archive checksum, checks its contents and binary size, then installs
atomically. Set `BAILOUT_VERSION=v0.2.0` or `BAILOUT_INSTALL_DIR=/your/bin` to override.
It prefers an existing writable PATH location and never uses sudo or modifies shell rc files.

## Website and API

- [Landing page](https://bailout.bailout-router.workers.dev)
- [User guide](https://bailout.bailout-router.workers.dev/docs/)
- [FastAPI reference](https://api.bailout-router.workers.dev/docs)
- [Live model list](https://api.bailout-router.workers.dev/v1/models)

The site uses self-hosted Space Grotesk and IBM Plex Mono, with their OFL licenses
included. No analytics, external fonts, or frontend framework. The visual language
is inspired by [neobrutalism.dev](https://www.neobrutalism.dev/).

## Host your own router

Use Python 3.13+, uv 0.12.3+, and Node (for Wrangler). Choose your own Worker name
in `api/wrangler.jsonc` before deploying.

```sh
cd api
uv sync
uv run pywrangler login
uv run pywrangler deploy
uv run pywrangler secret put OPENROUTER_API_KEY
```

Enter the OpenRouter key at the secret prompt. Never put it in source or config.
For edge development, place it in a gitignored `.dev.vars` file and run
`uv run pywrangler dev`. For ordinary local FastAPI development, set the key in
your environment and run `uv run uvicorn app:app --app-dir src --reload`.
The API exposes `/health`, `/v1/models`, `/v1/chat`, `/docs`, and `/openapi.json`.
Chat accepts `{model, messages, stream}`. With `stream: true`, it returns NDJSON
`model`, `text`, and `done` events, or an `error` event. Only the `done` event
contains a validated message that is safe to pass to the tool dispatcher.

To deploy the site, set the service binding in `worker/wrangler.jsonc` to your API
Worker, then run `npm ci && npm run deploy` in `worker`.

```sh
export BAILOUT_API_URL=https://your-worker.your-subdomain.workers.dev
bailout
```

`BAILOUT_MODEL` sets the default model. `BAILOUT_DEFAULT_API` at compile time changes
the binary's built-in backend URL. Runtime API overrides require HTTPS, except localhost
for development. The public service intentionally requires no login, so use your own
Worker and key if you need a separate quota. Cloudflare hosting limits are separate
from model prices; no paid infrastructure subscription is required by this repository.

MIT licensed. Inspired by [Pi](https://pi.dev/) and [fx](https://fx.sh/).
