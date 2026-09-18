#!/usr/bin/env python3
"""Record a real public-service session in a disposable, credential-free Linux box.

Run: uv run --with pyte python scripts/record-demo.py
First build: docker build -t bailout-launch-demo:local -f demo/Dockerfile demo
Requires Docker. Writes raw terminal output and an asciicast to artifacts/demo.
No responses, tool calls, downloads, or filesystem outcomes are simulated.
"""
import argparse
import codecs
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pty
import re
import select
import struct
import subprocess
import termios
import time

import pyte

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--scenario', choices=['setup', 'recovery'], default='setup')
args = parser.parse_args()
RECOVERY = args.scenario == 'recovery'
OUT = ROOT / ('artifacts/recovery-demo' if RECOVERY else 'artifacts/demo')
COLS, ROWS = 96, 26
NAME = 'bailout-recovery-recording' if RECOVERY else 'bailout-launch-recording'
OUT.mkdir(parents=True, exist_ok=True)
if (OUT/'session.cast').exists():
    raise SystemExit('Archive the previous recording before starting another take.')
(OUT/'verified.json').unlink(missing_ok=True)


class Recording:
    def __init__(self):
        self.screen = pyte.Screen(COLS, ROWS)
        self.stream = pyte.Stream(self.screen)
        self.decoder = codecs.getincrementaldecoder('utf-8')('replace')
        self.events = []
        self.started = time.monotonic()
        self.started_wall = int(time.time())
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack('HHHH', ROWS, COLS, 0, 0))
            os.execvp('docker', ['docker', 'run', '--rm', '-it', '--name', NAME,
                '--hostname', 'dev-box' if RECOVERY else 'fresh-box', '--cpus', '2', '--memory', '2g',
                '-e', 'PS1=\\[\\e[1;32m\\]\\u@\\h\\[\\e[0m\\]:\\w# ',
                '-e', "PROMPT_COMMAND=printf '\\033]133;D;%s\\007\\033]133;A\\007' \"$?\"",
                'bailout-recovery-demo:local' if RECOVERY else 'bailout-launch-demo:local'])
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack('HHHH', ROWS, COLS, 0, 0))
        self.raw = b''
        self.shell_markers = 0

    def pump(self, seconds=.1):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            ready, _, _ = select.select([self.fd], [], [], min(.05, max(0, end-time.monotonic())))
            if not ready:
                continue
            try:
                chunk = os.read(self.fd, 65536)
            except OSError as exc:
                raise EOFError('Recording terminal closed') from exc
            if not chunk:
                raise EOFError('Recording terminal closed')
            self.raw += chunk
            self.shell_markers += chunk.count(b'\x1b]133;A\x07')
            text = self.decoder.decode(chunk)
            self.events.append([round(time.monotonic()-self.started, 6), 'o', text])
            self.stream.feed(text)
            if b'\x1b[6n' in chunk:
                os.write(self.fd, f'\x1b[{self.screen.cursor.y+1};{self.screen.cursor.x+1}R'.encode())

    def until(self, predicate, timeout=600):
        deadline = time.monotonic()+timeout
        last_report = 0
        while time.monotonic() < deadline:
            self.pump()
            if predicate():
                self.pump(.3)
                return
            if time.monotonic()-last_report > 30:
                print('\n'.join(line.rstrip() for line in self.screen.display if line.strip())[-1600:], flush=True)
                last_report = time.monotonic()
        raise TimeoutError('\n'.join(self.screen.display))

    def mark(self, title):
        self.events.append([round(time.monotonic()-self.started,6), 'm', title])
        print('CHAPTER:', title, flush=True)

    def type(self, text):
        for char in text:
            os.write(self.fd, char.encode())
            self.pump(.028)
        self.pump(.35)
        os.write(self.fd, b'\r')

    def command(self, text, title=None, timeout=600, expected_exit=0):
        if title:
            self.mark(title)
        before = self.shell_markers
        self.type(text)
        self.until(lambda: self.shell_markers > before, timeout)
        self.pump(1.5)
        statuses = re.findall(rb'\x1b]133;D;(\d+)\x07', self.raw)
        if not statuses or int(statuses[-1]) != expected_exit:
            raise RuntimeError(f'Unexpected shell exit status for {text!r}: {statuses[-1:]}')

    def save(self):
        header = {'version':2,'width':COLS,'height':ROWS,'timestamp':self.started_wall,
                  'title': 'Bailout: repair your agent, then delete the harness' if RECOVERY else 'Bailout: install your tools, then delete the harness',
                  'env':{'TERM':'xterm-256color','SHELL':'/bin/bash'}}
        with (OUT/'session.cast').open('w') as f:
            f.write(json.dumps(header)+'\n')
            for event in self.events:
                f.write(json.dumps(event,ensure_ascii=False)+'\n')
        (OUT/'session.txt').write_bytes(self.raw)


rec = Recording()
verified = False
checks = []
try:
    rec.until(lambda: rec.shell_markers > 0, 30)
    if RECOVERY:
        rec.command('opencode run "Reply with: back online"', 'Your usual agent will not start', expected_exit=1)
        if b'Configuration is invalid' not in rec.raw:
            raise RuntimeError('The expected config failure was not reproduced')
    else:
        rec.command('opencode --version', 'Fresh box', expected_exit=127)
    rec.command('curl -fsSL https://bailout.dev/install.sh | bash', 'One curl')
    rec.mark('No API key. Just ask.')
    rec.type('bailout')
    rec.until(lambda: rec.screen.display[rec.screen.cursor.y].strip() == '›', 60)
    rec.pump(1)
    rec.type('OpenCode won\'t start: "Expected PermissionActionConfig, got confirm at permission.bash" in ./opencode.json. Back it up and fix it; keep my other settings.' if RECOVERY else
        'Install OpenCode from https://opencode.ai/install. Check its version, then stop. Skip sign-in.')
    rec.mark('Repair your usual agent' if RECOVERY else 'Set up your tools')
    rec.until(lambda: rec.screen.display[rec.screen.cursor.y].strip() == '›', 900)
    rec.pump(3)
    if any(message in rec.raw for message in (b'No free route completed', b'Stopped at 50 model steps',
            b'free model capacity is busy', b'No paid fallback was used.')):
        raise RuntimeError('Model turn did not finish cleanly; retain this take for debugging.')
    before = rec.shell_markers
    rec.type('/exit')
    rec.until(lambda: rec.shell_markers > before, 30)
    if RECOVERY:
        # Read the actual files back without asking the model to grade itself.
        def read_container(path):
            return subprocess.check_output(['docker', 'exec', NAME, 'cat', path], timeout=10)
        fixture = ROOT/'demo/recovery'
        original = (fixture/'opencode.json').read_bytes()
        repaired = json.loads(read_container('/root/project/opencode.json'))
        (OUT/'repaired-config.json').write_text(json.dumps(repaired, indent=2)+'\n')
        expected = json.loads(original)
        expected['permission']['bash'] = 'ask'
        assert repaired == expected, 'Repair changed unrelated settings or relaxed permissions'
        assert read_container('/root/.config/opencode/opencode.json') == (fixture/'global.json').read_bytes(), 'Global settings changed'
        assert read_container('/root/project/README.md') == (fixture/'README.md').read_bytes(), 'Project file changed'
        hashes = subprocess.check_output(['docker', 'exec', NAME, 'find', '/root',
            '-type', 'f', '-size', '-10k', '-exec', 'sha256sum', '{}', '+'], text=True).splitlines()
        digest = hashlib.sha256(original).hexdigest()
        backups = [line[66:] for line in hashes if line.startswith(digest+'  ')
                   and line[66:] != '/root/project/opencode.json']
        assert backups, 'Original config backup missing'
        checks = ['Real config failure reproduced', 'Only invalid permission value repaired',
                  'Original config backed up', 'Global settings and project file unchanged']
        (OUT/'verification.txt').write_text(json.dumps({'repaired_config':repaired, 'backups':backups}, indent=2)+'\n')
        rec.command('bailout uninstall', 'The harness meant to be deleted')
        output_before = len(rec.raw)
        rec.command('opencode run "Reply with: back online"', 'Back to your usual agent', timeout=180)
        # Require an actual response, not just a displayed command or a --version check.
        response = rec.raw[output_before:].decode('utf-8', errors='replace')
        response = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', response)
        assert re.search(r'(?im)^back online\s*$', response), 'OpenCode did not answer'
        checks.append('OpenCode answered through its free model after Bailout was removed')
    else:
        # A new login shell sees installer PATH changes; verify independently of
        # whatever the model claimed before recording the handoff and uninstall.
        check = subprocess.run(['docker','exec',NAME,'bash','-lc',
            'test -x /root/.opencode/bin/opencode && /root/.opencode/bin/opencode --version'],
            capture_output=True,text=True,timeout=60)
        (OUT/'verification.txt').write_text(check.stdout+check.stderr)
        if check.returncode:
            raise RuntimeError('Tool installation did not verify: '+check.stdout+check.stderr)
        rec.command('~/.opencode/bin/opencode --version', 'Your agent is ready')
        rec.command('bailout uninstall', 'The harness meant to be deleted')
        rec.command('~/.opencode/bin/opencode --version', 'Your tools stay')
        checks.append('OpenCode runs')
    rec.pump(3)
    check = subprocess.run(['docker','exec',NAME,'bash','-c',
        '! command -v bailout && test -x /root/.opencode/bin/opencode'],capture_output=True,text=True)
    if check.returncode:
        raise RuntimeError('Uninstall preservation check failed')
    checks += ['Bailout removed', 'Installed tools preserved']
    verified = True
    print('PASS: '+', '.join(checks),flush=True)
finally:
    rec.save()
    if verified:
        (OUT/'verified.json').write_text(json.dumps({
            'cast_sha256': hashlib.sha256((OUT/'session.cast').read_bytes()).hexdigest(),
            'scenario': args.scenario,
            'checks': checks,
        }, indent=2)+'\n')
    subprocess.run(['docker','stop','--time','2',NAME],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    os.close(rec.fd)
    os.waitpid(rec.pid,0)
