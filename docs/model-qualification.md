# Model qualification and context-aware routing

Version 0.7 uses this router in the hosted service. Only models with genuine passing
qualification evidence serve production requests. No fabricated bootstrap scores
or third-party ranking dataset is bundled.

## Context

Every model must advertise at least 32,768 tokens. Before every inference attempt,
the backend checks the actual candidate and its hosting endpoint, reserves response
space, and leaves 10% of the window or 2,048 tokens as headroom, whichever is larger.
Only fitting endpoints are sent to an inference broker. Input-token caps, when
reported by an endpoint, are checked separately. Account token quotas are another
independent admission check.

The client preserves the full conversation. The former 100,000-byte history cutoff
and silent removal of older turns are gone. When a preferred model gets too close
to its limit, Auto selects another qualified free model and emits a notice before
inference, without asking for confirmation. Completed tool calls and results stay
paired. Provider-specific opaque reasoning is removed when switching providers.
An explicit upstream context-length error also permits a switch and does not mark
the model globally unhealthy. If no qualified model fits, return `context_exhausted`
and preserve history; the user can start a fresh task with `/new`.

The first implementation uses a **conservative UTF-8 byte upper estimate**, including
tool schemas and framing, instead of assuming bytes/4. It can switch earlier than
an exact provider tokenizer would. It is not an exact token count or a guarantee
against every undocumented provider limit. The CLI contains no tokenizer tables.
There is a separate 4,000,000-byte HTTP-body ceiling and 2,048-message ceiling to
bound transport and parsing work. The CLI leaves space below that for the envelope.
No automatic conversation summarization is implemented.

Token-rate admission uses a separate estimate of two UTF-8 bytes per input token,
plus framing and the full output allowance, then reconciles reported actual usage.
This estimate can be wrong; upstream rate limits remain authoritative and may
return 429. It never relaxes context bounds or free-only billing checks. Applying
the one-token-per-byte context bound to rate quotas would reject ordinary coding
conversations well before their actual token allowance.

## Evidence, not model-name scoring

`bench/run.py` runs the actual Rust harness with its production system prompt and
native Bash tool against ten synthetic setup/repair scenarios. Validators check
filesystem state and run repaired programs in a second sandbox. Verbal claims of
success do not count. Test data contains no real credentials or user conversations.

Qualification requires:

- The same model metadata fingerprint and versioned suite.
- At least one complete ten-task run with 80% success, native Bash tool calls,
  and no critical failures. Once two runs exist, their combined pass rate must
  remain at least 80%.
- Evidence no older than 30 days. Context size and parameter count earn no quality
  points. Names and marketing descriptions do not affect the quality score.

These are initial task-fitness thresholds, not a general intelligence leaderboard.
The last two run summaries bound the influence of older success. A changed model
fingerprint starts over. Aliases whose provider does not expose version changes
still need recurring regression checks. Increment the suite version when changing
evaluation criteria incompatibly.

Provider outages, exhausted quota, missing inference connectivity, and incomplete
suites do not lower competence scores. An observed destructive action is recorded
even if the rest of a run cannot finish. Two subsequent complete clean runs are
needed to replace that evidence. Qualification is necessary but not sufficient:
live free eligibility, context, cooldowns, quotas, and the hosting allowance must
all pass before serving a user.

Every qualified model remains eligible to serve users. Rankings score task success
(0–100 points), recent availability (0–25 points), additional test evidence (up to
2 points after a second complete run), and a small successful-session preference
(2 points). The evidence bonus breaks close scores without excluding newly
qualified models. Availability can outweigh the difference between an 80% and
100% model; qualification establishes useful setup ability, not frontier performance.
The Durable Object keeps recent success/failure averages, latency, and cooldowns
per route, without prompts or conversation logs.
Old health penalties decay toward neutral. Unrecognized models receive no traffic
until qualified.

## Nightly job and isolation

`.github/workflows/qualify-models.yml` runs daily at 09:17 UTC and supports manual
dispatch. Manual runs accept up to two exact candidate IDs for bootstrap or
regression checks; blank input uses the same rotation as the scheduled job.
It evaluates up to two models with at most 80 inference API submissions per model
(160 per workflow), including retries and capacity refusals. Each candidate has
its own allowance so a throttled provider cannot starve the next model. The
controller stops starting new tasks after 45 minutes, leaving time to finish the
current bounded task and upload completed evidence before the 60-minute job limit.
Incomplete candidates never erase another candidate's completed results. The
gateway independently caps evaluation at 1,000 requests per UTC day, shared across
workflow reruns; production provider quotas and the hosting allowance still apply.
The authenticated evaluator allows up to 1,000 admissions per hour for release
verification, with the same 30-per-minute pacing and 1,000-per-day ceiling. Public
client limits are unchanged. Catalog reads also consume evaluator admissions.
Evaluation never switches models. The controller may retry the same conversation
twice for transient provider failures or short quota delays, honoring delays up to 65 seconds within a 165-second
request deadline. Every retry counts toward both evaluation and provider quotas.
Failed attempts are buffered and discarded; partial tool calls cannot run. Daily
quota exhaustion ends the run as inconclusive. Each result belongs to its candidate,
whose metadata fingerprint is rechecked before inference.
Working routes due for a weekly regression check are prioritized, followed by
newly qualified models due for a follow-up run. Other candidates rotate by oldest
evidence, with daily rotation of untested candidates so outages cannot strand the
queue. This bounded rotation does not reevaluate every model nightly.

The Bash container has no network, provider keys, GitHub token, Docker socket, or
publishing credentials. A Unix socket exposes a capped proxy for one model. The
controller outside that sandbox authenticates requests. Validators reject symlinks
and special files before reading modified fixtures on the host; generated programs
run only in a separate restricted container. Timed-out containers are removed.
The benchmark checks Linux setup tasks; normal harness CI covers all three supported
platforms. Linux benchmark scores are not claims of measured macOS task performance.

GitHub uploads only a small aggregate JSON artifact (seven-day retention). A separate
job with a different credential publishes it. The server validates the schema,
dates, model IDs and evidence, rejects older snapshots, and replaces the active
snapshot transactionally. Untested models remain in the previous valid snapshot;
live eligibility checks still apply. The previous snapshot is retained for recovery.
An empty/failed run cannot replace rankings.

`/v1/status` reports the ranking timestamp and `stale` after 48 hours. Workflow
failures appear in GitHub Actions; enable GitHub's workflow-failure notifications.
GitHub schedules are best effort and can disable after repository inactivity.
Staleness is visible but does not independently send an email from Cloudflare.

## Operator setup and rollout

1. Deploy the new gateway/DO code while leaving its `API` binding on the existing
   backend. Deploy the new Python code under a **separate private Worker name** and
   bind its `CAPACITY` to the same gateway. Keep workers.dev and preview URLs off.
2. Provision the desired provider secrets on the new Python Worker. Configure
   verified free-account attestations as described in [service limits](service-limits.md).
   A provider adapter existing in source does not mean its account is verified or
   any of its models are qualified.
3. Add an optional gateway service binding named `EVALUATOR` pointing to the new
   Python Worker. Normal requests continue using `API`; authenticated benchmark
   requests use `EVALUATOR`. Never expose the new Python Worker directly.
4. Generate two distinct random secrets of at least 32 characters. Store
   `BENCHMARK_TOKEN` and `RANKING_PUBLISH_TOKEN` in the gateway's encrypted secrets.
   Store their values as GitHub Actions secrets `BAILOUT_BENCHMARK_TOKEN` and
   `BAILOUT_RANKING_PUBLISH_TOKEN`, respectively. Never commit either value.
5. Run qualification and inspect the results. One complete run passing at least
   eight of ten tasks can qualify a model; later runs continue checking it.
   An empty registry intentionally rejects production inference;
   **do not move the public API binding until genuine passing evidence exists**.
   Verify free billing and current account limits before enabling each direct pool.
6. Switch the gateway's `API` binding to the qualified new backend. Keep its previous
   target available for rollback. Release the CLI through the three-platform workflow,
   verify installation and public binary sizes, then deploy the site build.

For local unit checks, run `python3 bench/test_bench.py`. On a Linux Docker host,
build the benchmark image and run `BAILOUT_TEST_DOCKER=1 python3 bench/test_bench.py`
to exercise all fixtures through the real CLI without calling any real model.
On Docker Desktop, run the trusted controller inside Linux with a named volume
mounted at `/evaluation`, set `TMPDIR=/evaluation` and `BAILOUT_EVAL_VOLUME` to that
volume's name, and supply the controller with Docker CLI access. That access is
never mounted into the model's sandbox.

No LiteLLM or Artificial Analysis runtime dependency is required. Our own synthetic
benchmark supplies qualification evidence; an external dataset would require its
own license review and explicit model-version mapping.
