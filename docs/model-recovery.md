# Automatic model recovery

Bailout uses Auto routing: no model picker or local provider setup. The hosted
backend chooses a working free route; the terminal reports recovery and the model
that answered. Older API clients may still pin an explicit free model. A pinned
request can retry that model, but never silently changes it.

## Recovery rules

One model response has at most **four upstream attempts** and a **120-second
end-to-end deadline**, including eligibility checks, capacity waits and retries.
Auto gives each inference at most 40 seconds when other qualified routes remain
to be tried. For the last remaining candidate, it allows up to 90 seconds, as does
a legacy pinned attempt. The overall request deadline still applies. There is no
unbounded retry loop. The CLI can make up to eight HTTP attempts within five
minutes for a structured temporary-capacity refusal. It displays each wait,
honors the server delay (up to 120 seconds), and uses increasing backoff. The
five-minute bound includes requests and waits; Ctrl-C cancels either immediately.
Authentication/policy failures, daily quota exhaustion, unknown pricing, invalid
responses and the hosting cutoff are not retried by the CLI.

- A temporary HTTP 429 gets one delayed retry of the same model. Respect numeric
  or HTTP-date `Retry-After`, add jitter, and wait at most 10 seconds for that retry.
  A longer delay moves to an eligible alternative instead of retrying early.
- An ambiguous 429 is not automatically called an exhausted account. It can retry
  once and then move to another model. Each attempt still consumes provider quota.
- A known account/daily quota or authentication failure disables that provider for
  the request. Auto can use a separately configured free provider; changing models
  or keys within the exhausted account does not create more quota.
- Timeouts, connection failures, broken streams and invalid responses can move to
  another eligible model. Policy refusals, invalid user input, length limits,
  unexpected charges and the hosting cutoff stop recovery.
- Unknown pricing disables the affected route. Every OpenRouter inference,
  including same-model retries, gets current model and endpoint checks and hard
  zero-price routing caps. Direct providers require an explicitly verified Free
  account with billing disabled where free eligibility comes from the account tier;
  zero-priced Z.AI and Vercel routes have their own live pricing checks.

A shared SQLite Durable Object reserves **every upstream attempt**, including
retries. It applies rolling request/token quotas, at most eight concurrent calls
per provider, and two per model (one for ZAI free routes). This prevents a burst from spending all available
slots on one failing model. Rejected and timed-out attempts remain counted;
successful reported token usage reconciles conservative reservations. Abandoned
concurrency leases expire after 120 seconds.

All qualified models can serve concurrent users. When a model has no free slot,
Auto immediately tries another eligible model, including a lower-scoring one on
the same provider. A full account quota instead requires an independent provider.
There is no top-model-only serving restriction or fixed candidate-list cutoff;
the four-attempt and total-time bounds still apply.

Busy routes get shared cooldowns, so the next user benefits from earlier failures.
The terminal also keeps a successful-model preference and short failure hints in
memory. Neither hints nor caller-supplied model IDs can override price checks,
provider quotas or the hosting budget. If all compatible pools are busy, production
requests enter bounded fair waiting. If none becomes usable within that wait,
return `free_capacity_exhausted` with the known retry delay. This is best-effort
capacity, not a guarantee of a free response or a complete session.

## Fair waiting

The shared Durable Object keeps at most 64 waiting model requests. Each receives a
random, private ticket for this response only. An older compatible request gets
priority over a newer request competing for the same provider pool. A request
whose route is unavailable or whose context cannot fit does not block a usable
alternative. Different pools and already admitted model calls remain concurrent.

One upstream attempt consumes a turn. A provider retry or the next response after
a Bash command joins the back, so an active session cannot repeatedly jump ahead
of newcomers. Internal queue polls do not reserve inference tokens or send model
requests. Pricing, qualification, context, and quotas are checked again before
the actual inference; waiting never overrides them.

The server polls admission about every five seconds and streams a readable wait
notice about every 15 seconds. Queue sleeps total at most 90 seconds within the
existing 120-second request deadline, leaving time for inference. Full or timed-out
queues return `free_capacity_exhausted` with a bounded retry hint. The CLI's
existing five-minute retry budget still applies. A later HTTP retry joins anew;
the service does not promise a permanent place across separate requests.

Completion and cancellation remove the ticket. An abandoned ticket expires after
30 seconds without activity, and no ticket survives 120 seconds. The queue stores
only random tickets, timestamps, route IDs, and token requirements, never prompts,
IP addresses, or persistent session/installation identifiers. Quality benchmarks
yield to compatible production waiters and retain their existing retry limits.

Completed Bash commands remain in history with their results. Recovery only retries
the unfinished model response; it never replays completed commands. Discard partial
tool calls. Strip provider-specific reasoning metadata when changing models while
preserving conversation text and completed tool results. Ordinary Bash errors go
back to the model to diagnose. Ctrl-C cancels recovery and local commands.

## Context and qualification

Every production route must pass the [Bailout benchmark](model-qualification.md).
Quality evidence and live availability are separate. A 32,768-token model window
is the minimum; a large window does not establish intelligence. Each attempt must
fit the complete input, reserved output, and 10%/2,048-token headroom. Estimates
currently conservatively count UTF-8 bytes plus tool schemas and framing, so a
switch can happen before an exact tokenizer would require one.

The CLI no longer silently drops old turns at 100,000 bytes. A `model` event with
`reason: "context_limit"`, `retry: true`, and a readable `notice` announces a switch
before the larger route is called. No confirmation is requested. `done.context`
reports the selected window, input upper estimate, reserved output, and margin.
Non-streaming responses include `notices` too. If no qualified free route fits,
`context_exhausted` preserves history and asks the user to start a new task. The
separate transport ceiling is 4 MB and 2,048 messages.

A provider's free token quota can be smaller than its model's context window.
If the request cannot fit even an empty token bucket, Auto tries another route
with `reason: "provider_token_limit"` and a notice, preserving the conversation.
If no route fits, it returns `context_exhausted`; waiting cannot fix that request.

## Client protocol

`POST /v1/chat` accepts `model`, `messages`, `stream`, and optional Auto hints:

```json
{
  "model": "auto",
  "preferred_model": "vendor/working:free",
  "avoid_models": ["vendor/failed:free"],
  "messages": [{"role": "user", "content": "Help repair my setup"}],
  "stream": true
}
```

Hints allow at most 35 route IDs and are rejected on pinned requests. If all routes
are hinted as failed, the server reconsiders them through its shared cooldown and
quota checks. The CLI clears stale failure hints only after a permitted capacity
wait; it resubmits the same unfinished conversation and never bypasses the meter.
Direct-provider route IDs are server aliases for verified free-tier
access, rather than upstream model identifiers or zero list-price claims.

Streaming returns NDJSON `model`, `text`, `done`, and `error` events. A `model`
event with `retry: true` and `notice` announces waiting or a discarded attempt.
Reset provisional output/tool assembly. Only one final `done` event authorizes
commands; use its validated complete `message`, never partial text. It includes
the successful model, provider, failed-model hints and cooldown. An `error` event
is terminal even under HTTP 200. Non-streaming clients receive a final JSON result
or a JSON error with the matching HTTP status and `Retry-After` when known.

No prompts, file contents or credentials are stored in the quota ledger. It retains
unlinked attempt timestamps, token reservations and cooldowns for at most a day,
cleaned on subsequent use. Raw provider errors are not exposed or logged.
See [service limits](service-limits.md) for the error contract and hosting budget.
