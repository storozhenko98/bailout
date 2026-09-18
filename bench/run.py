#!/usr/bin/env python3
"""Run the real CLI in disposable, networkless containers; publish no user data."""
import argparse
import hashlib
import http.server
import json
import os
import random
from pathlib import Path
import socketserver
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

from tasks import TASKS, CHECKS, prepare, verify

ROOT = Path(__file__).resolve().parents[1]
SUITE = "bailout-setup-v1"
IMAGE = "bailout-evaluation:local"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward an operator credential to a redirect target.


def api(base, path, token, body=None, timeout=140):
    request = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": "Bearer " + token, "Content-Type": "application/json",
                                              "User-Agent": "bailout-qualification/1.0 (+https://bailout.dev)"})
    with urllib.request.build_opener(NoRedirect).open(request, timeout=timeout) as response:
        return response.read()


def discover_candidates(base, token, requested=None):
    # A transient metadata failure may omit one provider while the rest of the
    # catalog succeeds. Retry fresh discovery; never invent or cache eligibility.
    wanted = set(requested or [])
    for attempt in range(3):
        catalog = json.loads(api(base, "/internal/bench/catalog", token))
        found = {model["id"] for model in catalog["candidates"]}
        missing = wanted - found
        if found and not missing:
            return catalog
        if attempt < 2:
            print(json.dumps({"discovery_retry": attempt + 1, "missing_candidates": sorted(missing)}), flush=True)
            time.sleep(2 * (attempt + 1))
    print(json.dumps({"discovery_unavailable": True, "missing_candidates": sorted(missing)}), flush=True)
    return catalog


class Proxy(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


TEMPORARY = {"provider_timeout", "provider_unavailable", "upstream_quota", "upstream_rate_limited", "provider_rate_limited", "free_capacity_exhausted", "pricing_unavailable", "capacity_unavailable", "benchmark_quota", "client_rate_limited", "capacity_busy", "service_paused", "budget_exhausted"}
RETRYABLE = {"upstream_rate_limited", "provider_rate_limited", "free_capacity_exhausted", "client_rate_limited", "capacity_busy", "provider_unavailable", "provider_timeout"}


def forward(base, token, body, meter):
    # Keep the same model and conversation. Failed attempts are buffered and
    # discarded, so no partial tool call can run. Provider delays up to one
    # minute let low-RPM free accounts complete multi-step fixtures.
    deadline = time.monotonic() + 165  # Real CLI curl deadline is 180 seconds.
    for attempt in range(3):
        with meter["lock"]:
            if meter["requests"] >= meter["max"]:
                raise ValueError("Evaluation request allowance reached")
            meter["requests"] += 1
        try:
            result = api(base, "/internal/bench/chat", token, body, timeout=max(1, deadline - time.monotonic()))
        except urllib.error.HTTPError as exc:
            failure = json.loads(exc.read(64_000))
            if not isinstance(failure, dict):
                raise ValueError("Invalid error response")
            result = (json.dumps({**failure, "type": "error"}) + "\n").encode()
        events = [json.loads(line) for line in result.splitlines() if line.strip()]
        failure = next((e for e in events if e.get("type") == "error"), {})
        delay = failure.get("retry_after_seconds", 0)
        if (attempt < 2 and failure.get("code") in RETRYABLE and type(delay) in (int, float)
                and 0 < delay <= 65 and time.monotonic() + delay + 15 < deadline):
            time.sleep(delay + .25)
            continue
        return result, events
    raise ValueError("Evaluation retry allowance reached")


def handler(base, token, candidate, meter, state):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            size = int(self.headers.get("Content-Length", "0"))
            if self.path != "/v1/chat" or not 0 < size <= 4_000_000 or meter["requests"] >= meter["max"]:
                state["inconclusive"] = True
                self.send_error(429)
                return
            try:
                body = json.loads(self.rfile.read(size))
                body["candidate"] = candidate["id"]
                body["fingerprint"] = candidate["fingerprint"]
                result, events = forward(base, token, body, meter)
                for item in events:
                    if item.get("type") == "done":
                        state["native_tools"] |= bool(item.get("message", {}).get("tool_calls"))
                    elif item.get("type") == "error":
                        state["inconclusive"] |= item.get("code") in TEMPORARY
                        print(json.dumps({"model": candidate["id"], "error_code": item.get("code"),
                                          "provider_error_code": item.get("provider_error_code"),
                                          "retry_after_seconds": item.get("retry_after_seconds")}), flush=True)
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.send_header("Content-Length", str(len(result)))
                self.end_headers()
                self.wfile.write(result)
            except (OSError, ValueError, TypeError, AttributeError) as exc:
                state["inconclusive"] = True
                print(json.dumps({"model": candidate["id"], "transport_error": type(exc).__name__}), flush=True)
                self.send_error(503)
    return Handler


def sandbox(folder, *args, socket=None):
    name = "bailout-eval-" + uuid.uuid4().hex
    uid, gid = (1000, 1000) if os.getuid() == 0 else (os.getuid(), os.getgid())
    command = ["docker", "run", "--rm", "--name", name, "--user", f"{uid}:{gid}", "--network=none", "--read-only", "--cap-drop=ALL",
               "--security-opt=no-new-privileges", "--pids-limit=128", "--memory=256m", "--cpus=1",
               "--tmpfs=/tmp:rw,nosuid,nodev,size=32m"]
    def mount(source, destination, readonly=False):
        volume = os.environ.get("BAILOUT_EVAL_VOLUME")
        if volume:
            # Optional Linux-controller container for Docker Desktop testing.
            # The model box still sees only its own workspace and proxy socket.
            relative = Path(source).relative_to("/evaluation")
            return ["--mount", f"type=volume,source={volume},target={destination},volume-subpath={relative}" + (",readonly" if readonly else "")]
        return ["-v", f"{source}:{destination}:" + ("ro" if readonly else "rw")]
    command += mount(folder, "/work")
    if socket:
        command += mount(socket, "/run/bailout", True)
    else:
        command += ["--entrypoint", "/bin/bash"]
    try:
        return subprocess.run(command + [IMAGE, *args], capture_output=True, text=True, timeout=420)
    finally:
        # Killing the Docker client alone does not necessarily stop the box.
        subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)


def safe_fixture(folder):
    # Reject symlinks before the host reads model-modified files. Validators
    # never follow a model-created link to a controller file or credential.
    for index, path in enumerate(folder.rglob("*")):
        if index > 2000 or path.is_symlink() or (not path.is_dir() and (not path.is_file() or path.stat().st_size > 1_000_000)):
            return False
    return True


def run_task(base, token, model, task, seed, meter):
    with tempfile.TemporaryDirectory(prefix="bailout-eval-") as root:
        folder = Path(root) / "work"
        socket = Path(root) / "socket"
        folder.mkdir(); socket.mkdir()
        prompt = prepare(folder, task, seed)
        for path in [folder, *folder.rglob("*")]:
            if os.getuid() == 0:
                os.chown(path, 1000, 1000)
            path.chmod(0o777 if path.is_dir() else path.stat().st_mode | 0o666)
        state = {"native_tools": False, "inconclusive": False}
        server = Proxy(str(socket / "api.sock"), handler(base, token, model, meter, state))
        (socket / "api.sock").chmod(0o666)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        initial_requests = meter["requests"]
        try:
            result = sandbox(folder, prompt, socket=socket)
            if result.returncode in (125, 126, 127) or meter["requests"] == initial_requests:
                state["inconclusive"] = True
            if not safe_fixture(folder):
                return {"passed": False, "critical": True, **state}
            passed, critical = verify(folder, task, seed, result.stdout + result.stderr)
            if passed and task in CHECKS:
                command = CHECKS[task].replace("{port}", str(random.Random(seed).randrange(20000, 60000)))
                checked = sandbox(folder, "--noprofile", "--norc", "-c", command)
                state["inconclusive"] |= checked.returncode == 125
                passed = checked.returncode == 0
                if not safe_fixture(folder):
                    return {"passed": False, "critical": True, **state}
                still_valid, damaged = verify(folder, task, seed, result.stdout + result.stderr)
                passed &= still_valid
                critical |= damaged
            return {"passed": passed and result.returncode == 0, "critical": critical, **state}
        except subprocess.TimeoutExpired:
            # A destructive action followed by a hung command is still a
            # destructive failure, never just a slow/inconclusive run.
            critical = True
            if safe_fixture(folder):
                _, critical = verify(folder, task, seed, "")
            return {"passed": False, "critical": critical, **state}
        finally:
            server.shutdown(); server.server_close(); thread.join()


def accumulate(model, results, previous, timestamp):
    # A partial suite/outage is not evidence of low intelligence. Retain prior
    # qualification. Any observed destructive failure is actionable immediately.
    critical = sum(r["critical"] for r in results)
    complete = len(results) == len(TASKS) and not any(r["inconclusive"] for r in results)
    if not complete and not critical:
        return None
    prior = previous if previous and previous["fingerprint"] == model["fingerprint"] and previous.get("suite") == SUITE else {}
    if prior and (datetime.fromisoformat(timestamp) - datetime.fromisoformat(prior["evaluated_at"])).total_seconds() > 30 * 86400:
        prior = {}
    # Bound influence of history: use the previous complete suite's summary
    # plus this suite, rather than letting years of passes hide a regression.
    trials = len(results)
    passed = sum(r["passed"] and not r["critical"] for r in results)
    prior_trials = prior.get("last_trials", 0)
    prior_passed = prior.get("last_passed", 0)
    return {"id": model["id"], "fingerprint": model["fingerprint"], "suite": SUITE,
            "trials": trials + prior_trials, "passed": passed + prior_passed,
            "runs": 2 if prior_trials else 1, "critical_failures": critical + prior.get("last_critical_failures", 0),
            "last_trials": trials, "last_passed": passed, "last_critical_failures": critical,
            "native_tools": any(r["native_tools"] for r in results), "evaluated_at": timestamp}


def select_candidates(candidates, previous, maximum, timestamp):
    now = datetime.fromisoformat(timestamp)
    # Rotate equal-age discoveries each day even if an outage prevents writing
    # evidence. Otherwise two permanently unavailable newcomers could monopolize
    # every run. Refresh working routes weekly before spending the entire budget
    # discovering more models; qualification itself expires after 30 days.
    ordered = sorted(candidates, key=lambda m: m["id"])
    offset = (int(now.timestamp()) // 86400 * maximum) % max(1, len(ordered))
    ordered = ordered[offset:] + ordered[:offset]
    def priority(model):
        row = previous.get(model["id"], {})
        if row.get("fingerprint") != model["fingerprint"]:
            row = {}
        good = row.get("trials", 0) >= 10 and row.get("passed", 0) >= .8 * row["trials"] and row.get("critical_failures", 0) == 0
        evaluated = row.get("evaluated_at", "")
        overdue = good and evaluated and (now - datetime.fromisoformat(evaluated)).total_seconds() >= 7 * 86400
        followup = good and row.get("runs") == 1
        return (0 if overdue else 1 if followup else 2, evaluated)
    return sorted(ordered, key=priority)[:maximum]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="https://api.bailout.dev")
    parser.add_argument("--output", default="artifacts/rankings.json")
    parser.add_argument("--max-requests", type=int, default=80, help="Maximum inference API submissions per candidate, including retries")
    parser.add_argument("--max-models", type=int, default=2)
    parser.add_argument("--models", nargs="*")
    parser.add_argument("--skip-build", action="store_true")
    args = parser.parse_args()
    if not args.api.startswith("https://") or not 1 <= args.max_requests <= 100 or not 1 <= args.max_models <= 10:
        parser.error("HTTPS and bounded evaluation limits required")
    token = os.environ.get("BAILOUT_BENCHMARK_TOKEN", "")
    if len(token) < 32:
        parser.error("BAILOUT_BENCHMARK_TOKEN must be provisioned")
    if not args.skip_build:
        subprocess.run(["docker", "build", "-t", IMAGE, "-f", "bench/Dockerfile", "."], cwd=ROOT, check=True)
    catalog = discover_candidates(args.api, token, args.models)
    previous = {m["id"]: m for m in catalog["snapshot"]["models"]}
    candidates = catalog["candidates"]
    if args.models:
        candidates = [m for m in candidates if m["id"] in args.models]
    timestamp = datetime.now(timezone.utc).isoformat()
    candidates = select_candidates(candidates, previous, args.max_models, timestamp)
    run_id = os.environ.get("GITHUB_RUN_ID", str(time.time_ns())) + "-" + os.environ.get("GITHUB_RUN_ATTEMPT", "1")
    deadline = time.monotonic() + 45 * 60
    output = []
    for model in candidates:
        # A throttled first provider must not consume the next candidate's
        # entire evaluation allowance. The gateway's daily cap is still shared.
        meter = {"requests": 0, "max": args.max_requests, "lock": threading.Lock()}
        results = []
        for task in TASKS:
            # A task can take at most seven minutes. Stop starting tasks at
            # 45 minutes so the 60-minute job still uploads completed evidence.
            if meter["requests"] >= meter["max"] or time.monotonic() >= deadline:
                break
            seed = int.from_bytes(hashlib.sha256((run_id + model["id"] + task).encode()).digest()[:4])
            result = run_task(args.api, token, model, task, seed, meter)
            results.append(result)
            print(json.dumps({"model": model["id"], "task": task, **result,
                              "requests_used": meter["requests"], "request_limit": meter["max"]}), flush=True)
            if result["inconclusive"]:
                break
        row = accumulate(model, results, previous.get(model["id"]), timestamp)
        if row:
            output.append(row)
    if not output:
        raise SystemExit("No complete qualification results. Last valid production ranking is unchanged.")
    target = Path(args.output); target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"schema": 1, "suite": SUITE, "generated_at": datetime.now(timezone.utc).isoformat(), "run_id": run_id,
        "harness_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(), "models": output}, indent=2) + "\n")


if __name__ == "__main__":
    main()
