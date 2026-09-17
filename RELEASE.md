# bailout v0.4.0

The harness meant to be deleted. Bootstrap a fresh Mac or Linux VM, or repair your broken coding setup, then hand back to your usual tools.

- Checks for a newer stable release at startup, verifies the checksum, replaces itself atomically and restarts. Failed checks retain the current installation. `BAILOUT_NO_UPDATE=1` opts out; `bailout update` checks manually.
- Public API fair-use limits and a shared persistent hosting allowance. A documented `budget_exhausted` error pauses new work; the CLI displays it without automatic retries.
- FastAPI stays on Cloudflare behind a private service binding and a global admission guard. Only verified free OpenRouter models are eligible.

```sh
curl -fsSL https://bailout.dev/install.sh | bash
bailout
```

Older versions need this installer once to gain automatic updates. macOS ARM64, Linux x64 and Linux ARM64 only. No local API key, account, Git, Node or Python required. Bash is the only model tool and runs automatically with your permissions.

[Website](https://bailout.dev) · [Guide](https://bailout.dev/docs/) · [Limits and error contract](https://github.com/storozhenko98/bailout/blob/main/docs/service-limits.md)
