# bailout

**The harness meant to be deleted.**

You have a fresh VM or a new Mac. No GitHub CLI, no configured coding agent,
no API key handy. Or your usual agent broke, and you need a working one to fix it.

Bailout gets you a small, independent harness in one command. Use it to bootstrap
the machine or repair your setup, get back to your normal tools, then delete it.

```sh
curl -fsSL https://bailout.dev/install.sh | bash
bailout
```

No account, local API key, Git, Node, Python, or existing agent required.
Apple Silicon macOS, x64 Linux, and ARM64 Linux. Bring Bash, curl, internet access,
and the usual base utilities (including tar and a SHA-256 utility).

[Website](https://bailout.dev) · [Setup & recovery guide](https://bailout.dev/docs/) · [Releases](https://github.com/storozhenko98/bailout/releases/latest)

## When you need it

**A clean slate.** Boot an EC2 instance, a GCP VM, or a new Mac. Ask bailout to
inspect the machine, install missing tools, help you sign in to GitHub, clone your
repo, and get Codex, Claude Code, Pi, or OpenCode ready to use.

**A broken setup.** Your normal agent no longer starts. A config is malformed, a
runtime moved, or an update broke something. Use an independent harness to inspect
what changed, back up the config, repair it, and verify that your main tool works.

**A quick exit.** The useful outcome is your usual setup working again. Bailout has
no daemon, account setup, persistent conversation store, or project scaffolding.
It is one native binary: **622.0 KB on Apple Silicon** in v0.4.0. Every release stays under
6,000,000 bytes. Linux releases are statically linked with musl.

```text
$ bailout

  bailout v0.4.0
  ~

  › auto · free models · full access
  Fresh machine? Broken setup? Tell me what needs to work.
  /shell local terminal   /model choose a model   /help

  › this is a fresh Ubuntu VM. help me set up gh and OpenCode
```

Other starting points:

```sh
bailout 'pi stopped launching after a config change. diagnose and repair it'
bailout 'check what is missing before I can use my usual dev tools here'
```

Bash is the only model tool. Commands run automatically with your account's
permissions, including file changes, package installation, and network access.
This is not a sandbox. The agent is instructed to inspect first, preserve working
setup, back up configuration before repair, and verify the result.

## Sign in locally

Bailout provides its own free inference. The tools you set up still use **your own
accounts and credentials**.

For sign-in, sudo, or key entry, the Bash tool can hand you the real terminal with
`interactive: true`. That command's input and output are not captured for the model;
only its exit status is returned. You can also enter `/shell` for a local Bash
session, then type `exit` to return. Don't paste passwords or API keys into chat.
Normal Bash output is captured, so never ask the agent to print credential files.

For example, [GitHub CLI's login flow](https://cli.github.com/manual/gh_auth_login)
can run in the local terminal handoff. Authentication still needs your participation.
Bailout cannot manufacture an account or recover an unavailable secret.

## Done? Delete it.

```sh
bailout uninstall
```

This removes only the running bailout binary. The tools you installed, repositories
you cloned, and configurations you repaired remain in place. It does not remove
other tools, credentials, or directories. Reinstall with the same curl command
whenever you need it again.

## Controls

| In a session | Action |
| --- | --- |
| `/model` | Pick a currently available free model |
| `/models` | List free models and live provider health |
| `/model auto` | Return to automatic selection |
| `/model vendor/model:free` | Pin a free model |
| `/shell` | Open local Bash for private or interactive setup; `exit` returns |
| `/last` | Expand the last command's captured output |
| `/new` | Start a fresh conversation |
| `/help` / `/exit` | Show help or quit |

Ctrl-C stops a model request or Bash process group. At the prompt, it clears a draft
or exits when empty. Up/Down browses history; Ctrl-J or Alt-Enter inserts a newline.
Replies stream as they arrive. Questions can be answered directly, without a Bash
call. Commands execute only after their complete tool call is validated.

Each Bash call starts a fresh shell in the session directory unless `workdir` is
specified. Shell state does not persist between calls. Ordinary commands default
to a two-minute timeout; interactive commands default to ten minutes. Models can
request up to thirty minutes. `--max-steps N` changes the default 50 model steps.
`--model ID` and `BAILOUT_MODEL` select a model. `NO_COLOR=1` disables colors.

Bailout's Bash calls skip shell startup files, and its curl transport ignores
`.curlrc`, so those customizations do not have to work before bailout can help.

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
the app never pays to bypass it. The public gateway enforces 30 requests/minute, 300/hour and 1,000/day per IP,
plus a globally shared allowance. IPv6 /64s and users behind a NAT share limits.
At the hosting cutoff, HTTP 503 `budget_exhausted` explains when capacity returns;
the CLI displays it without automatically retrying. See [limits and error codes](docs/service-limits.md).

Source contracts: [OpenRouter provider routing](https://openrouter.ai/docs/guides/routing/provider-selection),
[OpenRouter limits](https://openrouter.ai/docs/api/reference/limits),
[Cloudflare Workers pricing](https://developers.cloudflare.com/workers/platform/pricing/).

## Privacy

Prompts, model-selected file contents, and captured Bash output are sent through the hosted
Worker to OpenRouter and its selected inference provider. Provider data policies apply;
free does not mean zero data retention. The Worker does not store conversations or
log request bodies. The gateway retains short-lived daily IP hashes and aggregate quota counters; see [retention details](docs/service-limits.md). Worker observability is disabled. Do not include credentials in
prompts or ask the agent to read secret files.

## Measured release sizes

From the [published v0.4.0 assets](https://github.com/storozhenko98/bailout/releases/tag/v0.4.0), verified against SHA-256 checksums:

| Platform | Native binary (uncompressed) | Download (.tar.gz) |
| --- | ---: | ---: |
| macOS ARM64 | 622,048 bytes | 315,190 bytes |
| Linux x64 | 840,480 bytes | 413,457 bytes |
| Linux ARM64 | 790,784 bytes | 394,345 bytes |

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
For the recovery use case: `python3 scripts/live-recovery.py target/release/bailout`
diagnoses a broken fixture agent, backs up its configuration, and verifies repair.

Three direct Rust dependencies: `serde_json`, `libc`, and `rustyline`. System curl
handles TLS; Bash handles everything the model does. The API is FastAPI on
Cloudflare Python Workers. A separate Worker serves the static landing page and
forwards legacy API URLs through the budget gateway. The Python service is private;
a SQLite Durable Object reserves capacity atomically before forwarding requests.

The smoke test drives a real controlling terminal. It verifies Ctrl-C as a keypress
while editing, waiting on a model, running a child process, and at the empty prompt,
plus history, multiline input, model selection, local interactive authentication
without credential capture, shell handoff, uninstall, and recovery after interruption.

CI runs the tests and real binary smoke checks on all three supported platforms.
Tagging `vX.Y.Z` builds native release assets, tests them, enforces the size ceiling,
and publishes SHA-256 checksums. The installer pins one resolved release version,
verifies the archive checksum, checks its contents and binary size, then installs
atomically. Set `BAILOUT_VERSION=v0.4.0` or `BAILOUT_INSTALL_DIR=/your/bin` to override.
It prefers an existing writable PATH location and never uses sudo or modifies shell rc files.

Starting in v0.4.0, each launch checks the official GitHub stable release. A newer
release is downloaded, SHA-256 verified, installed atomically, and restarted with
the same arguments, working directory, input and backend setting. Failed checks
keep the existing binary. Use `bailout update` to check manually or
`BAILOUT_NO_UPDATE=1` to disable automatic updates. Custom-backend builds manage
their own updates. Older releases need the installer once to gain this feature.

## Website and API

- [Landing page](https://bailout.dev)
- [User guide](https://bailout.dev/docs/)
- [FastAPI reference](https://api.bailout.dev/docs)
- [Live model list](https://api.bailout.dev/v1/models)

The site uses self-hosted Space Grotesk and IBM Plex Mono, with their OFL licenses
included. No analytics, external fonts, or frontend framework. The visual language
is inspired by [neobrutalism.dev](https://www.neobrutalism.dev/).

## Host your own router

Use Python 3.13+, uv 0.12.3+, and Node (for Wrangler). Choose your own Worker name
in `api/wrangler.jsonc` before deploying. Keep Python private. In
`worker/gateway.wrangler.jsonc`, choose your gateway name and domain and point its
`API` binding at your Python Worker. Keep workers.dev and preview URLs disabled.
Review [the budget policy and operating instructions](docs/service-limits.md).

The Python API needs **Workers Paid** on Cloudflare. Its JSON processing and
streaming exceed the Free plan's 10 ms CPU allowance; requests can otherwise be
terminated midway through a reply. Paid starts at $5/month plus usage. This is
hosting cost, separate from the strict zero-cost model routing. See
[Cloudflare's current pricing](https://developers.cloudflare.com/workers/platform/pricing/)
and [CPU limits](https://developers.cloudflare.com/workers/platform/limits/).
You can also run the same FastAPI app on an existing server with Uvicorn.

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

Deploy the guard with `npm ci && npx wrangler deploy --config gateway.wrangler.jsonc`
in `worker`. To deploy the site, point the service binding in `worker/wrangler.jsonc`
at your gateway Worker, then run `npm run deploy`. Set your own domains in both
configs. The public gateway additionally exposes `/v1/status`.

```sh
export BAILOUT_API_URL=https://api.your-domain.example
bailout
```

`BAILOUT_MODEL` sets the default model. `BAILOUT_DEFAULT_API` at compile time changes
the binary's built-in backend URL. Runtime API overrides require HTTPS, except localhost
for development. The public service intentionally requires no login, so use your own
Worker and key if you need a separate quota. Cloudflare hosting costs and limits are
separate from model prices.

MIT licensed. Inspired by [Pi](https://pi.dev/) and [fx](https://fx.sh/).
