# bailout v0.5.0

The harness meant to be deleted. Bootstrap a fresh machine or repair your broken
coding setup, then hand back to your usual tools.

- Auto recovers from provider failures, timeouts, broken streams and invalid
  responses, trying up to three verified-free models within 120 seconds.
- Successful models stick for the terminal session. Failed models get a temporary
  cooldown. The terminal announces recovery and preserves completed Bash work.
- Explicit model selections stay pinned. Account limits, policy blocks, pricing
  uncertainty and the hosting cutoff stop recovery with a clear error.
- Every attempt still verifies free pricing; no paid fallback, credentials or
  telemetry are added to the harness.

Restart bailout v0.4+ to update automatically, or install:

```sh
curl -fsSL https://bailout.dev/install.sh | bash
bailout
```

macOS ARM64, Linux x64 and Linux ARM64. No local API key, account, Git, Node or
Python needed. Bash is the only model tool and runs with your permissions.

[Website](https://bailout.dev) · [Recovery behavior](https://github.com/storozhenko98/bailout/blob/main/docs/model-recovery.md) · [Guide](https://bailout.dev/docs/)
