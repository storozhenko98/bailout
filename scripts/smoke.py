#!/usr/bin/env python3
"""Exercise the real binary, HTTP transport, Bash tool, and follow-up turn offline."""
import http.server
import json
import os
import pathlib
import signal
import subprocess
import sys
import tempfile
import threading
import time

binary = pathlib.Path(sys.argv[1]).resolve()
calls = []
mode = "edit"


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        calls.append(body)
        assert self.path == "/v1/chat"
        assert body["model"] == "auto"
        if body["messages"][-1]["role"] == "tool":
            tool_result = json.loads(body["messages"][-1]["content"])
            assert tool_result["exit_code"] == 0
            message = {"role": "assistant", "content": "Created and verified hello.txt."}
        else:
            command = "printf 'hello bailout\\n' > hello.txt; cat hello.txt"
            if mode == "interrupt":
                command = "sleep 30 & echo $! > child.pid; wait"
            message = {"role": "assistant", "content": None, "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "bash", "arguments": json.dumps({"command": command})}}]}
        data = json.dumps({"model": "test/coder:free", "message": message}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
env = {**os.environ, "BAILOUT_API_URL": f"http://127.0.0.1:{server.server_port}", "BAILOUT_MODEL": "auto"}
with tempfile.TemporaryDirectory(prefix="bailout-smoke-") as folder:
    result = subprocess.run([binary, "create hello.txt"], cwd=folder, env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert pathlib.Path(folder, "hello.txt").read_text() == "hello bailout\n"
    assert "Created and verified" in result.stdout
    assert len(calls) == 2
    mode = "interrupt"
    process = subprocess.Popen([binary, "wait"], cwd=folder, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    pidfile = pathlib.Path(folder, "child.pid")
    for _ in range(100):
        if pidfile.exists():
            break
        time.sleep(0.05)
    assert pidfile.exists(), "Bash child was never started"
    process.send_signal(signal.SIGINT)
    process.communicate(timeout=5)
    assert process.returncode == 130
    # Linux can briefly retain a killed orphan as a zombie until init reaps it.
    child_pid = pidfile.read_text().strip()
    ps = subprocess.run(["ps", "-o", "stat=", "-p", child_pid], capture_output=True, text=True)
    assert not ps.stdout.strip() or ps.stdout.strip().startswith("Z"), f"Child survived interruption: {ps.stdout}"
server.shutdown()
print("PASS: real HTTP → Bash edit → tool result → final answer; Ctrl-C kills child processes")
