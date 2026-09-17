First release of bailout: a tiny, fully automatic terminal coding agent.

- One model tool: unrestricted Bash.
- Interactive sessions, one-shot prompts, piped prompts, and model selection.
- Live free-model and provider pricing verification before every inference.
- Zero-price caps, explicit provider allowlists, and free-only fallback.
- Apple Silicon macOS, x64 Linux, and arm64 Linux. Linux builds use static musl.
- Every release binary must be below 6,000,000 bytes; installer verifies SHA-256.

Requires Bash and curl. No local API key, Node, Python, or package manager needed.
Hosted free capacity is shared and best effort. Commands execute immediately with your user permissions.

```sh
curl -fsSL https://raw.githubusercontent.com/storozhenko98/bailout/main/install.sh | bash
bailout
```
