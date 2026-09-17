Bailout v0.2.0 makes the terminal experience usable and gives the project a home.

- Direct conversation without mandatory Bash calls.
- Streamed replies, an editable prompt, Unicode, history, multiline input, and a model picker.
- Ctrl-C clears a draft, exits an empty prompt, or stops an active model request and Bash process group.
- Compact command output with `/last` to expand the captured result.
- A real FastAPI backend on its own API host, with interactive API docs.
- A responsive landing page and user guide inspired by neobrutalism.dev.
- Fresh free-price and provider-health checks on every model request. No paid fallback.
- Apple Silicon, Linux x64, and Linux ARM64; every binary stays below 6 MB.

[Website](https://bailout.bailout-router.workers.dev) · [Guide](https://bailout.bailout-router.workers.dev/docs/) · [API](https://api.bailout-router.workers.dev/docs)

Install or update:

```sh
curl -fsSL https://bailout.bailout-router.workers.dev/install.sh | bash
bailout
```

Requires Bash and curl. Commands run automatically with your permissions.
Hosted free capacity is shared and best effort.
