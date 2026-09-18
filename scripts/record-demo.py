#!/usr/bin/env python3
"""Record a real public-service session in a disposable, credential-free Linux box.

Run: uv run --with pyte python scripts/record-demo.py
First build: docker build -t bailout-launch-demo:local -f demo/Dockerfile demo
Requires Docker. Writes raw terminal output and an asciicast to artifacts/demo.
No responses, tool calls, downloads, or filesystem outcomes are simulated.
"""
import codecs
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pty
import select
import struct
import subprocess
import termios
import time

import pyte

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'artifacts/demo'
COLS, ROWS = 96, 26
NAME = 'bailout-launch-recording'
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
                '--hostname', 'fresh-box', '--cpus', '2', '--memory', '2g',
                '-e', 'PS1=\\[\\e[1;32m\\]root@fresh-box\\[\\e[0m\\]:\\w# ',
                '-e', "PROMPT_COMMAND=printf '\\033]133;A\\007'",
                'bailout-launch-demo:local'])
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

    def command(self, text, title=None, timeout=600):
        if title:
            self.mark(title)
        before = self.shell_markers
        self.type(text)
        self.until(lambda: self.shell_markers > before, timeout)
        self.pump(1.5)

    def save(self):
        header = {'version':2,'width':COLS,'height':ROWS,'timestamp':self.started_wall,
                  'title':'Bailout: install your tools, then delete the harness',
                  'env':{'TERM':'xterm-256color','SHELL':'/bin/bash'}}
        with (OUT/'session.cast').open('w') as f:
            f.write(json.dumps(header)+'\n')
            for event in self.events:
                f.write(json.dumps(event,ensure_ascii=False)+'\n')
        (OUT/'session.txt').write_bytes(self.raw)


rec = Recording()
verified = False
try:
    rec.until(lambda: rec.shell_markers > 0, 30)
    rec.command('opencode --version', 'Fresh box')
    rec.command('curl -fsSL https://bailout.dev/install.sh | bash', 'One curl')
    rec.mark('No API key. Just ask.')
    rec.type('bailout')
    rec.until(lambda: rec.screen.display[rec.screen.cursor.y].strip() == '›', 60)
    rec.pump(1)
    rec.type('Install OpenCode from https://opencode.ai/install. Check its version, then stop. Skip sign-in.')
    rec.mark('Set up your tools')
    rec.until(lambda: rec.screen.display[rec.screen.cursor.y].strip() == '›', 900)
    rec.pump(3)
    if b'No free route completed' in rec.raw or b'Stopped at 50 model steps' in rec.raw:
        raise RuntimeError('Model turn did not finish cleanly; retain this take for debugging.')
    before = rec.shell_markers
    rec.type('/exit')
    rec.until(lambda: rec.shell_markers > before, 30)
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
    rec.pump(3)
    check = subprocess.run(['docker','exec',NAME,'bash','-c',
        '! command -v bailout && test -x /root/.opencode/bin/opencode'],capture_output=True,text=True)
    if check.returncode:
        raise RuntimeError('Uninstall preservation check failed')
    verified = True
    print('PASS: real install, public Auto inference, verified tools, removed only Bailout.',flush=True)
finally:
    rec.save()
    if verified:
        (OUT/'verified.json').write_text(json.dumps({
            'cast_sha256': hashlib.sha256((OUT/'session.cast').read_bytes()).hexdigest(),
            'checks': ['OpenCode runs', 'Bailout removed', 'Installed tools preserved'],
        }, indent=2)+'\n')
    subprocess.run(['docker','stop','--time','2',NAME],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    os.close(rec.fd)
    os.waitpid(rec.pid,0)
