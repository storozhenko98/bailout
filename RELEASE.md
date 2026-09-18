# bailout v0.7.3

Wait through temporary free-provider capacity limits without losing the task.

- The terminal explains temporary capacity waits and retries the unfinished response with backoff, within five minutes and eight HTTP attempts. Ctrl-C cancels immediately.
- Completed Bash commands stay in history and are never replayed by the wait loop. Real-terminal tests cover cancellation and exactly-once execution across a retry.
- Daily quota, authentication, policy, pricing and hosting-budget failures still stop promptly. Every retry passes the same free-pricing and shared quota checks.
- Provider qualification jobs now resume interrupted suites while retaining completed passes and failures. Only complete qualifying results enter production routing.

Restart bailout v0.4+ to update automatically, or install:

```sh
curl -fsSL https://bailout.dev/install.sh | bash
bailout
```

macOS ARM64, Linux x64 and Linux ARM64. No local API key, account, Git, Node or Python needed.

[Website](https://bailout.dev) · [Guide](https://bailout.dev/docs/)
