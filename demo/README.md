# Recording the setup demo

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

Published assets live in `site/demo/`. Use a new filename version when replacing a
published recording because those assets have immutable cache headers. Keep the
homepage, GitHub preview, poster and demo page links consistent.
