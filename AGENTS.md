# What bailout is

Bailout is **the harness meant to be deleted**: temporary help for a fresh machine
or a broken primary coding setup. The core journey is install, bootstrap or repair,
verify the user's usual tools work, hand back, and remove bailout.

Lead with that purpose in product copy, examples, and design choices. A smaller
agent for everyday coding is not the product positioning. Avoid features that turn
it into a permanent workspace, account system, or replacement for the user's agent.

# Constraints

- macOS ARM64, Linux x64, and Linux ARM64 only. Native release binaries <6,000,000 bytes.
- One model tool: Bash. Full auto by default. No dependency on Git, Node, Python,
  another agent, or a local API key to get started.
- Free OpenRouter models only. Recheck model and endpoint prices before every
  inference, including fallback. Unknown pricing fails closed; keep zero-price caps.
- Keep sign-in and credential entry in interactive terminal handoffs, outside the
  model conversation. Do not capture their terminal input or output.
- Uninstall removes only the bailout binary. Never delete the user's installed
  tools, configuration, credentials, repositories, or unrelated directories.

# Verification

Run Rust tests and clippy, API policy tests, gateway tests, size checks, installer
checks, and scripts/smoke.py against the real release binary. The terminal smoke
must send real keystrokes through a controlling PTY, including Ctrl-C during input,
model requests, Bash, and interactive sign-in. Keep secret-entry and uninstall tests.
Review site changes at desktop and mobile sizes. Release through the three-platform
GitHub workflow; verify public assets and installation before calling it shipped.
