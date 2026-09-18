# bailout v0.6.0

Automatic routing for a fresh machine or a broken coding setup.

- Auto-only terminal: no model picker or provider setup before getting help.
- Temporary 429s retry once with delay and jitter; failed routes get shared cooldowns.
- Every inference attempt, including retries, reserves shared request/token capacity.
  Concurrency limits keep bursts from flooding one model. Recovery is bounded to
  four attempts and 120 seconds, with clear capacity errors.
- Optional Groq Free adapter adds an independent provider pool when explicitly
  configured on a Free account with billing disabled. No paid fallback.
- Completed Bash work is preserved across recovery; partial tool calls never run.
- Startup updates, interactive secret-entry handoffs and safe uninstall remain.

Restart bailout v0.4+ to update automatically, or install:

```sh
curl -fsSL https://bailout.dev/install.sh | bash
bailout
```

macOS ARM64, Linux x64 and Linux ARM64. No local API key, account, Git, Node or
Python needed. Bash is the only model tool and runs with your permissions.

[Website](https://bailout.dev) · [Recovery behavior](https://github.com/storozhenko98/bailout/blob/main/docs/model-recovery.md) · [Guide](https://bailout.dev/docs/)
