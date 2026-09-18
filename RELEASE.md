# bailout v0.7.2

Fix a terminal hang found while bootstrapping a real fresh Linux box.

- Captured commands run in their own session, without access to the controlling terminal. Nested shell probes such as `bash -i -c 'command -v opencode'` can finish instead of waiting forever for terminal ownership.
- Ctrl-C still cancels the entire captured process group. Explicit interactive sign-in and local shell handoffs retain the real terminal.
- A controlling-PTY regression check covers the shell probe alongside cancellation, private credential entry, and uninstall checks.
- Includes v0.7.1's instructions to read official installation sources, verify the exact requested tool, and diagnose failures before retrying.

Restart bailout v0.4+ to update automatically, or install:

```sh
curl -fsSL https://bailout.dev/install.sh | bash
bailout
```

macOS ARM64, Linux x64 and Linux ARM64. No local API key, account, Git, Node or Python needed.

[Website](https://bailout.dev) · [Guide](https://bailout.dev/docs/)
