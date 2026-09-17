# Model recovery

Auto is for getting a working agent quickly. An explicit `/model vendor/model:free`
selection remains pinned, even when that model fails.

## Recovery rules

The backend allows at most **three provider attempts per model response**, under
one **120-second deadline** including pricing checks. Auto gives each inference
up to 40 seconds; pinned inference gets up to 90 seconds within the same deadline.
It skips providers without verified free pricing, tool support and healthy status.
Every fallback fetches fresh model and endpoint prices and retains zero-price caps.

Auto can recover from transport timeouts, provider outages, clearly provider-scoped
rate limits, broken streams, empty replies and malformed Bash calls. Account/key
quotas, unknown-scope rate limits, authentication failures, access/content-policy
blocks, invalid input, length limits and unexpected billed cost are terminal.
Pricing metadata failures also stop inference. Recovery does not change privacy,
provider permissions, model pricing, or the gateway's spending and fair-use limits.

The CLI remembers a successful Auto model for its process/session and avoids
failed models for five minutes, or longer for a numeric provider `Retry-After`
(up to 24 hours). These hints are held only in memory, are never identity or
authorization, and cannot make a paid/unhealthy model eligible. There is no
cross-user health database, session ID, prompt log or telemetry request. Closing
the process clears the hints. Explicitly choosing a model ignores Auto hints.

Completed commands remain in conversation history with their results. Recovery
retries only the unfinished model response, discards its partial tool calls, and
never reruns completed Bash commands. Model-specific reasoning signatures are
removed when switching models, while conversation text and tool results remain.
Ordinary Bash failures are passed to the model to diagnose; they do not trigger a
model switch by themselves. Ctrl-C cancels recovery and pending local commands.

## Client protocol

`POST /v1/chat` accepts `model`, `messages`, `stream`, and these optional Auto hints:

```json
{
  "model": "auto",
  "preferred_model": "vendor/working:free",
  "avoid_models": ["vendor/failed:free"],
  "messages": [{"role": "user", "content": "Help repair my setup"}],
  "stream": true
}
```

`avoid_models` permits at most 35 IDs. Hints are rejected on pinned requests.
The backend owns retry limits; the CLI does not automatically resubmit requests
after an API/transport error and multiply that limit.

Streaming returns NDJSON. An initial `model` event identifies the current attempt.
When an attempt fails, a `model` event with `retry: true`, `notice`, `failed_models`
and `cooldown_seconds` marks its output as discarded. Clear provisional tool
assembly, separate any displayed partial text, and wait for the next attempt.
These additive fields remain readable by older clients; v0.5 adds the recovery
notice and session hints.

Only one final `done` event can authorize tool execution. Use its complete
`message`; never assemble executable tool calls from partial text. It includes
the successful `model`, `failed_models`, and `cooldown_seconds`. An `error` event
is terminal even under HTTP 200. Non-streaming clients receive the same final
metadata or a JSON error with an appropriate HTTP status. Safe error codes are
documented in [service limits](service-limits.md); raw provider bodies, which may
contain prompts or internal details, are not exposed or logged.

Public provider health is an eligibility signal, not a guarantee of account
access. If all eligible models or the shared service are unavailable, bailout
stops clearly; it cannot repair an upstream account or hosting outage by switching
models.
