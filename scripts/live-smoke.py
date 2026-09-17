#!/usr/bin/env python3
"""Optional live check. Uses shared free quota and creates files only in a temp dir."""
import pathlib
import subprocess
import sys
import tempfile

binary = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else 'target/release/bailout').resolve()
prompt = (
    'Write answer.txt containing exactly 42 followed by a newline, then verify its '
    'contents using Bash. Work only in the current directory. Do not read any other '
    'directory or environment variables. Report the result briefly.'
)
with tempfile.TemporaryDirectory(prefix='bailout-live-') as folder:
    result = subprocess.run([binary, '--max-steps', '6', prompt], cwd=folder,
                            capture_output=True, text=True, timeout=240)
    print(result.stdout)
    print(result.stderr)
    output = pathlib.Path(folder, 'answer.txt')
    assert result.returncode == 0, f'Agent exited {result.returncode}'
    assert output.exists(), 'Agent claimed success without creating the file'
    assert output.read_bytes() == b'42\n', 'Incorrect file contents'
    assert '$ ' in result.stderr, 'No Bash execution was observed'
print('PASS: public backend → real model → local Bash write → verified file')
