Bailout is the harness meant to be deleted.

Use it when you have a fresh Mac or Linux VM with no agent or API key configured—or when your main coding setup breaks and you need an independent harness to fix it. Get your normal tools running, then remove bailout.

- Setup and recovery instructions: inspect the machine, preserve working configuration, back up before repair, verify, and hand back to your usual tools.
- Interactive Bash handoff for sign-in, sudo, and key entry. Its terminal input and output are not sent to the model.
- `/shell` opens local Bash; `exit` returns to bailout.
- `bailout uninstall` removes only the binary, leaving your tools and configuration in place.
- Streamed replies, prompt editing, history, multiline input, model picker, and working Ctrl-C.
- Free-only routing with fresh pricing and provider-health checks on every inference.
- Apple Silicon macOS: 604,704 bytes; Linux x64: 807,712 bytes; Linux ARM64: 725,248 bytes. All sizes are the full uncompressed binary.

[Website](https://bailout.dev) · [Setup & recovery guide](https://bailout.dev/docs/) · [FastAPI reference](https://api.bailout.dev/docs)

```sh
curl -fsSL https://bailout.dev/install.sh | bash
bailout
```

No local API key, account, Git, Node, or Python required. Bring Bash, curl, and base system utilities. Commands run automatically with your permissions. Hosted free capacity is shared and best effort.
