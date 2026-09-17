#!/usr/bin/env python3
"""Optional live recovery check; changes only an isolated fixture, uses free quota."""
import pathlib
import subprocess
import sys
import tempfile

binary = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else 'target/release/bailout').resolve()
with tempfile.TemporaryDirectory(prefix='bailout-recovery-') as folder:
    root = pathlib.Path(folder)
    original = 'MODE=broken\nKEEP_ME=original\n'
    (root/'agent.conf').write_text(original)
    launcher = root/'primary-agent'
    source = '''#!/usr/bin/env bash
cd "$(dirname "$0")" || exit 1
. ./agent.conf
if [ "$MODE" != ready ]; then
  echo 'Configuration error: MODE must be ready' >&2
  exit 2
fi
printf 'primary-agent ready\\n'
'''
    launcher.write_text(source); launcher.chmod(0o755)
    assert subprocess.run([launcher], capture_output=True).returncode == 2
    prompt = ('My primary-agent stopped working after a config change. Use a compact diagnosis, repair, and verification sequence. Work only in this '
              'directory. Run ./primary-agent to diagnose it, back up agent.conf before '
              'repairing it, preserve KEEP_ME and do not modify primary-agent. Verify '
              'the launcher works, then tell me how to return to it. Do not inspect '
              'environment variables or other directories.')
    result = subprocess.run([binary, '--max-steps', '8', prompt], cwd=root,
                            capture_output=True, text=True, timeout=300)
    print(result.stdout); print(result.stderr)
    assert result.returncode == 0, f'Agent exited {result.returncode}'
    assert launcher.read_text() == source, 'Launcher changed instead of configuration'
    assert 'KEEP_ME=original' in (root/'agent.conf').read_text()
    assert subprocess.run([launcher], capture_output=True).returncode == 0
    assert any(p.is_file() and p.name != 'agent.conf' and p.read_text(errors='replace') == original for p in root.iterdir()), 'Original configuration was not backed up'
print('PASS: live model diagnosed a broken agent, preserved a config backup and unrelated setting, and restored its launcher')
