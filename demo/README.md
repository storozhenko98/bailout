# Recording the demos

## Recovery demo (primary)

The primary demo starts with OpenCode 1.18.31 already installed. Its project
config contains a deliberately invalid Bash permission value, `confirm`, which
causes a real configuration error before OpenCode can answer. This is a prepared,
reproducible failure, not a claimed spontaneous incident. No model responses or
repair commands are scripted. Bailout receives only a plain-language request to
fix the config, back it up, and preserve the other settings. The final prompt
includes the observed startup error, invalid value and file path; it does not
provide the replacement value or repair command.

```sh
docker build -t bailout-launch-demo:local -f demo/Dockerfile demo
docker build -t bailout-recovery-demo:local -f demo/Dockerfile.recovery demo
uv run --with pyte python scripts/record-demo.py --scenario recovery
python3 scripts/render-demo.py --scenario recovery
```

The fixture in `demo/recovery/` contains only example data. No host directories,
credentials, or provider keys enter the container. The configured OpenCode model
is Big Pickle, listed as free in [OpenCode Zen pricing](https://opencode.ai/docs/zen/#pricing)
at recording time. Its companion small-model setting uses the same model. Check
the current pricing and no-key availability again before a new recording; free
offers can change. Bailout uses the public installer and ordinary Auto routing.

Independent checks require the initial config failure, exactly the intended
permission repair (`confirm` → `ask`), an unchanged copy of the original config
as a backup, unchanged global settings and project README, removal of Bailout,
and an actual OpenCode model response after removal. A version check alone is
insufficient. Every recorded shell command has its exit status checked. The raw
capture, verification manifest and render intermediates go to ignored
`artifacts/recovery-demo/`. Assets use the `bailout-demo-v2` filename.

The selected recording is 288.2 seconds before wait compression, 71.9 seconds
afterward. It includes repeated inspections and an Auto model switch. Earlier
takes hit provider timeouts or temporary capacity refusals; this is a selected
successful session, not a claim about typical speed or reliability.

## Setup demo (previous recording)

This is a real public-service session, recorded through a controlling terminal in
a disposable Ubuntu 24.04 container. It has Bash, curl, CA certificates, tar and
gzip. No provider keys, host directories, GitHub CLI or OpenCode are injected.
The recorder runs the public installer and leaves Auto routing and startup updates
on. Sign-in is explicitly skipped.

Requirements on the recording machine: Docker, Python with `pyte`, `agg`, FFmpeg
and FFprobe. Menlo is used for the terminal; the site's bundled fonts frame it.

```sh
docker build -t bailout-launch-demo:local -f demo/Dockerfile demo
uv run --with pyte python scripts/record-demo.py
python3 scripts/render-demo.py
```

Raw terminal output and the asciicast go into ignored `artifacts/demo/`. Archive
an existing take before recording another. Failed takes do not get a verification
manifest. Rendering requires the SHA-256 hash of the independently verified take.
The checks run OpenCode in a new shell, remove Bailout, and check that OpenCode
remains. They do not verify account authentication.

The renderer keeps every output event, accelerates spinner waits 16x, and caps
other idle gaps at 1.5 seconds. It never substitutes responses or command output.
The footer and page disclose the shortened waits. Review the video visually before
publishing it, including the versions and uninstall result. A selected successful
session is not a reliability benchmark; free models can still make mistakes.

Published assets live in `site/demo/`. The previous setup recording remains at
`bailout-demo-v1.*`. Use a new filename version when replacing a
published recording because those assets have immutable cache headers. Keep the
homepage, GitHub preview, poster and demo page links consistent.
