#!/usr/bin/env python3
"""Real binary + HTTP + controlling terminal. Ctrl-C is a key, not kill(SIGINT)."""
import fcntl
import http.server
import json
import os
import pathlib
import pty
import select
import signal
import shutil
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time

binary = pathlib.Path(sys.argv[1]).resolve()
calls = []
gateway_failures = 0
model_wait = threading.Event()
release_wait = threading.Event()


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_): pass
    def do_GET(self):
        self.send_json(dict(models=[dict(id='test/coder:free', name='Test Coder', available=True)]))
    def send_json(self, value):
        raw = json.dumps(value).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)
    def do_POST(self):
        global gateway_failures
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        calls.append(body)
        assert self.path == '/v1/chat'
        assert body['model'] == 'auto'
        prompt = next(m['content'] for m in reversed(body['messages']) if m['role'] == 'user')
        if prompt in ('budget exhausted', 'client limited'):
            self.send_response(503 if prompt == 'budget exhausted' else 429)
            self.send_header('Content-Type', 'application/json'); self.send_header('Retry-After','3600'); self.end_headers()
            self.wfile.write(json.dumps(dict(error='Shared hosting allowance exhausted. Capacity returns later. https://bailout.dev/docs/#service-limits',code='budget_exhausted' if prompt == 'budget exhausted' else 'client_rate_limited',retry_after_seconds=3600)).encode())
            return
        if prompt == 'gateway retry' and gateway_failures == 0:
            gateway_failures += 1
            self.send_response(503); self.end_headers()
            self.wfile.write(b'Temporary gateway failure')
            return
        if prompt == 'wait model':
            model_wait.set()
            release_wait.wait(15)
        if body['messages'][-1]['role'] == 'tool':
            message = dict(role='assistant', content='Local step complete.' if prompt in ('local login','cancel login') else 'Created and verified hello.txt.')
        elif prompt in ('local login', 'cancel login'):
            command = 'read -r -s -p "Local token: " token; test -n "$token"; printf "\\nLocal auth complete\\n"'
            message = dict(role='assistant', content=None, tool_calls=[dict(id='local_1', type='function', function=dict(name='bash', arguments=json.dumps(dict(command=command, interactive=True))))])
        elif prompt in ('create hello.txt', 'wait bash', 'broken stream'):
            command = "printf 'hello bailout\\n' > hello.txt; cat hello.txt"
            if prompt == 'wait bash': command = 'sleep 30 & echo $! > child.pid; wait'
            message = dict(role='assistant', content=None, tool_calls=[dict(id='call_1', type='function', function=dict(name='bash', arguments=json.dumps(dict(command=command))))])
        else:
            message = dict(role='assistant', content='Reply: '+prompt)
        response = dict(model='test/coder:free', message=message)
        if not body.get('stream'):
            if prompt == 'broken stream':
                self.send_response(503); self.end_headers(); return
            return self.send_json(response)
        events = [dict(type='model', model=response['model'])]
        if message.get('content'):
            events.extend(dict(type='text', text=chunk) for chunk in [message['content'][:4], message['content'][4:]])
        if prompt not in ('broken stream', 'recover stream', 'truncated http'): events.append(dict(type='done', **response))
        raw = ''.join(json.dumps(e)+'\n' for e in events).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/x-ndjson')
        self.send_header('Content-Length', str(len(raw) + (50 if prompt == 'truncated http' else 0)))
        self.end_headers()
        try: self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError): pass


class Terminal:
    def __init__(self, folder, term="xterm-256color"):
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.chdir(folder)
            os.execve(str(binary), [str(binary)], {**env, "TERM": term})
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack('HHHH', 32, 100, 0, 0))
        self.buffer = b''
        self.closed = False
        # The welcome banner also contains "  › ". Wait past it so tests send
        # input to the editor, not to startup before cancellation is reset.
        self.expect('/shell local terminal   /model choose a model   /help')
    def send(self, text): os.write(self.fd, text.encode() if isinstance(text, str) else text)
    def expect(self, text, timeout=10):
        needle = text.encode()
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            if needle in self.buffer:
                before, self.buffer = self.buffer.split(needle, 1)
                return before
            ready, _, _ = select.select([self.fd], [], [], .05)
            if ready:
                try: chunk = os.read(self.fd, 65536)
                except OSError: chunk = b''
                if not chunk: raise AssertionError(f'Terminal exited waiting for {text!r}: {self.buffer!r}')
                self.buffer += chunk
                if b'\x1b[6n' in chunk: self.send(b'\x1b[1;1R')
        raise AssertionError(f'Timeout waiting for {text!r}: {self.buffer[-2000:]!r}')
    def prompt(self): self.expect('  › ')
    def exit(self):
        deadline = time.monotonic()+5
        while time.monotonic() < deadline:
            ready, _, _ = select.select([self.fd], [], [], .02)
            if ready:
                try: chunk = os.read(self.fd, 65536)
                except OSError: chunk = b''
                if b'\x1b[6n' in chunk: self.send(b'\x1b[1;1R')
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid:
                assert os.waitstatus_to_exitcode(status) == 0, status
                self.closed = True
                os.close(self.fd)
                return
            time.sleep(.02)
        raise AssertionError('Ctrl-C did not exit the idle editor')
    def cleanup(self):
        if not self.closed:
            os.kill(self.pid, signal.SIGKILL)
            os.close(self.fd)
            os.waitpid(self.pid, 0)


server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
env = {**os.environ, 'BAILOUT_API_URL': f'http://127.0.0.1:{server.server_port}', 'BAILOUT_MODEL':'auto', 'TERM':'xterm-256color', 'NO_COLOR':'1', 'BAILOUT_NO_UPDATE':'1'}
with tempfile.TemporaryDirectory(prefix='bailout-smoke-') as folder:
    def run(prompt):
        return subprocess.run([binary, prompt], cwd=folder, env=env, capture_output=True, text=True, timeout=15)
    hello = run('hello')
    assert hello.returncode == 0 and hello.stdout == 'Reply: hello\n', (hello.stdout, hello.stderr)
    assert '$ ' not in hello.stderr
    gateway = run('gateway retry')
    assert gateway.returncode == 0 and 'Reply: gateway retry' in gateway.stdout
    assert gateway_failures == 1
    for prompt in ['budget exhausted', 'client limited']:
        before = len(calls)
        refused = run(prompt)
        assert refused.returncode == 1 and 'Shared hosting allowance exhausted' in refused.stderr
        assert 'https://bailout.dev/docs/#service-limits' in refused.stderr
        assert len(calls) == before + 1, 'Policy refusal was retried'
    recovered = run('recover stream')
    assert recovered.returncode == 0 and 'Reply: recover stream' in recovered.stdout
    assert 'Recovering a complete response' in recovered.stderr
    truncated = run('truncated http')
    assert truncated.returncode == 0 and 'Reply: truncated http' in truncated.stdout
    assert 'Recovering a complete response' in truncated.stderr
    broken = run('broken stream')
    assert broken.returncode == 1 and not pathlib.Path(folder, 'hello.txt').exists()
    result = run('create hello.txt')
    assert result.returncode == 0, result.stderr
    assert pathlib.Path(folder, 'hello.txt').read_text() == 'hello bailout\n'
    assert 'Created and verified' in result.stdout
    # A fresh-machine process: no Git/gh/Node/Python/agent/key on PATH, and broken
    # personal startup hooks must not prevent bailout's own transport or Bash.
    minimal_bin = pathlib.Path(folder, 'minimal-bin')
    minimal_home = pathlib.Path(folder, 'minimal-home')
    minimal_bin.mkdir(); minimal_home.mkdir()
    for name in ['bash', 'curl', 'cat']:
        (minimal_bin/name).symlink_to(shutil.which(name))
    (minimal_home/'.curlrc').write_text('this-is-an-invalid-curl-option\n')
    startup = minimal_home/'broken-startup'
    startup.write_text('exit 99\n')
    minimal_env = {'PATH': str(minimal_bin), 'HOME': str(minimal_home),
                   'BASH_ENV': str(startup), 'BAILOUT_API_URL': env['BAILOUT_API_URL'], 'BAILOUT_NO_UPDATE':'1'}
    fresh = subprocess.run([binary, 'create hello.txt'], cwd=folder, env=minimal_env,
                           capture_output=True, text=True, timeout=15)
    assert fresh.returncode == 0, fresh.stderr
    assert 'Created and verified' in fresh.stdout
    terminal = Terminal(folder)
    try:
        terminal.prompt()
        terminal.send('discard this')
        terminal.expect('discard this')
        terminal.send(b'\x03')
        terminal.expect('\x1b[?2004l')
        terminal.prompt()
        terminal.send('hélloX\x7f\r')
        terminal.expect('Reply: héllo')
        terminal.prompt()
        terminal.send(b'\x1b[A\r')
        terminal.expect('Reply: héllo')
        terminal.prompt()
        terminal.send('one\x0atwo\r')
        terminal.expect('Reply: one')
        terminal.expect('two')
        terminal.prompt()
        assert calls[-1]['messages'][-1]['content'] == 'one\ntwo'
        terminal.send('/model\r')
        terminal.expect('model › ')
        terminal.send('0\r')
        terminal.expect('Model: auto')
        terminal.prompt()
        terminal.send('local login\r')
        terminal.expect('Local token: ')
        terminal.send('fixture-secret-value\r')
        terminal.expect('Local step complete.')
        terminal.prompt()
        assert all('fixture-secret-value' not in json.dumps(c) for c in calls)
        assert 'not captured' in calls[-1]['messages'][-1]['content']
        terminal.send('cancel login\r')
        terminal.expect('Local token: ')
        terminal.send(b'\x03')
        terminal.expect('Stopped.')
        terminal.prompt()
        terminal.send('/shell\r')
        terminal.expect('This shell is not sent to the model.')
        terminal.send("printf 'SHELL_OK\\n'\r")
        terminal.expect('SHELL_OK\r\n')
        terminal.send('exit\r')
        terminal.expect('Back in bailout')
        terminal.prompt()
        terminal.send('wait model\r')
        terminal.expect('Thinking')
        assert model_wait.wait(5), 'No pending model request'
        terminal.send(b'\x03')
        terminal.expect('Stopped.')
        terminal.prompt()
        release_wait.set()
        terminal.send('wait bash\r')
        terminal.expect('Running Bash')
        pidfile = pathlib.Path(folder, 'child.pid')
        deadline = time.monotonic()+5
        while not pidfile.exists() and time.monotonic() < deadline: time.sleep(.02)
        assert pidfile.exists(), 'Bash child never started'
        terminal.send(b'\x03')
        terminal.expect('Stopped.')
        terminal.prompt()
        ps = subprocess.run(['ps', '-o', 'stat=', '-p', pidfile.read_text().strip()], capture_output=True, text=True)
        assert not ps.stdout.strip() or ps.stdout.strip().startswith('Z'), f'Child survived: {ps.stdout}'
        terminal.send('/new\r')
        terminal.expect('Fresh conversation.')
        terminal.prompt()
        terminal.send('hello again\r')
        terminal.expect('Reply: hello again')
        terminal.prompt()
        assert len(calls[-1]['messages']) == 2
        terminal.send(b'\x03')
        terminal.exit()
    finally: terminal.cleanup()
    # Interrupt as soon as the plain prompt is visible, including the gap before
    # its first read. Repetition catches the lost-signal race on fast Linux hosts.
    for _ in range(20):
        basic = Terminal(folder, term='dumb')
        try:
            basic.prompt()
            basic.send(b'\x03')
            basic.exit()
        finally: basic.cleanup()
    disposable = pathlib.Path(folder, 'bailout-to-remove')
    retained = pathlib.Path(folder, 'keep-my-config')
    retained.write_text('keep this')
    shutil.copy2(binary, disposable)
    removed = subprocess.run([disposable, 'uninstall'], capture_output=True, text=True, timeout=5)
    assert removed.returncode == 0 and not disposable.exists()
    assert retained.read_text() == 'keep this'
server.shutdown()
print('PASS: minimal fresh-machine environment, streamed chat without Bash, file edit, partial stream rejection, real Ctrl-C while editing/model/Bash/idle, history, Unicode, multiline, model picker, local login without credential capture, interactive cancellation, local shell, uninstall, recovery')
