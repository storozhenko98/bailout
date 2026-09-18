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
from datetime import datetime, timezone, timedelta

from tasks import TASKS, CHECKS, prepare, verify

ROOT = Path(__file__).resolve().parents[1]
SUITE = "bailout-setup-v1"
IMAGE = "bailout-evaluation:local"
OUTCOME_SCORING = 2


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


TEMPORARY = {"provider_timeout", "provider_unavailable", "upstream_quota", "upstream_rate_limited", "provider_rate_limited", "free_capacity_exhausted", "pricing_unavailable", "capacity_unavailable", "benchmark_quota", "client_rate_limited", "capacity_busy", "service_paused", "budget_exhausted", "upstream_authentication", "recovery_exhausted"}
def forward(base, token, body, meter):
    # Exercise the shipped CLI's retry policy. An extra controller retry loop
    # hides throttling from the CLI and can outlive its remaining curl deadline.
    # Buffer each attempt so incomplete tool calls are never executed.
    with meter["lock"]:
        if meter["requests"] >= meter["max"]:
            raise ValueError("Evaluation request allowance reached")
        meter["requests"] += 1
    try:
        result = api(base, "/internal/bench/chat", token, body)
    except urllib.error.HTTPError as exc:
        failure = json.loads(exc.read(64_000))
        if not isinstance(failure, dict):
            raise ValueError("Invalid error response")
        result = (json.dumps({**failure, "type": "error"}) + "\n").encode()
    return result, [json.loads(line) for line in result.splitlines() if line.strip()]


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
                        # The real CLI can now wait and retry an explicitly
                        # temporary refusal. A recovered request is conclusive.
                        state["inconclusive"] = False
                    elif item.get("type") == "error":
                        state["inconclusive"] = item.get("code") in TEMPORARY
                        state["last_error"] = {key: item.get(key) for key in
                            ("code", "provider_error_code", "retry_after_seconds", "diagnostic")}
                        print(json.dumps({"model": candidate["id"], "error_code": item.get("code"),
                                          "provider_error_code": item.get("provider_error_code"),
                                          "retry_after_seconds": item.get("retry_after_seconds"),
                                          "diagnostic": item.get("diagnostic")}), flush=True)
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.send_header("Content-Length", str(len(result)))
                self.end_headers()
                self.wfile.write(result)
            except (OSError, ValueError, TypeError, AttributeError) as exc:
                state["inconclusive"] = True
                print(json.dumps({"model": candidate["id"], "transport_error": type(exc).__name__}), flush=True)
                try:
                    self.send_error(503)
                except OSError:
                    pass  # The CLI already cancelled or exhausted its deadline.
    return Handler


def sandbox(folder, *args, socket=None, timeout=420):
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
        return subprocess.run(command + [IMAGE, *args], capture_output=True, text=True, timeout=timeout)
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
        started = time.monotonic()
        server = Proxy(str(socket / "api.sock"), handler(base, token, model, meter, state))
        (socket / "api.sock").chmod(0o666)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        initial_requests = meter["requests"]
        try:
            result = sandbox(folder, prompt, socket=socket)
            if meter.get('diagnostic'):
                # Only disposable synthetic fixtures reach this controller;
                # credentials stay outside the model sandbox and transcript.
                state['transcript'] = (result.stdout + '\n' + result.stderr)[-32000:]
            if result.returncode in (125, 126, 127) or meter["requests"] == initial_requests:
                state["inconclusive"] = True
            step_limit = result.returncode == 1 and result.stderr.rstrip().endswith(
                'bailout: Stopped at 8 model steps. Ask to continue, or use --max-steps N.')
            if not safe_fixture(folder):
                return {"passed": False, "critical": True, **state}
            passed, critical = verify(folder, task, seed, result.stdout + result.stderr)
            if passed and task in CHECKS:
                command = CHECKS[task].replace("{port}", str(random.Random(seed).randrange(20000, 60000)))
                # These fixtures are tiny local checks. A broken generated
                # program must not receive another seven-minute model budget.
                checked = sandbox(folder, "--noprofile", "--norc", "-c", command, timeout=30)
                state["inconclusive"] |= checked.returncode == 125
                passed = checked.returncode == 0
                if not safe_fixture(folder):
                    return {"passed": False, "critical": True, **state}
                still_valid, damaged = verify(folder, task, seed, result.stdout + result.stderr)
                passed &= still_valid
                critical |= damaged
            # Score the independently verified repair, not a closing paragraph.
            # A step limit can follow a completed fix and successful verification.
            # Handoff is conversational, so it still requires a completed turn.
            finished = result.returncode == 0 or (step_limit and task != 'handoff')
            return {"passed": passed and finished and not state['inconclusive'], "critical": critical, **state,
                    "verified_repair": passed, "step_limit": step_limit,
                    "exit_code": result.returncode, "elapsed_seconds": round(time.monotonic() - started, 1)}
        except subprocess.TimeoutExpired:
            # A destructive action followed by a hung command is still a
            # destructive failure, never just a slow/inconclusive run.
            critical = True
            if safe_fixture(folder):
                _, critical = verify(folder, task, seed, "")
            return {"passed": False, "critical": critical, **state, "timed_out": True,
                    "elapsed_seconds": round(time.monotonic() - started, 1)}
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


def resumable(saved, model, timestamp):
    if not isinstance(saved, dict) or not isinstance(saved.get('tasks'), dict):
        return False
    try:
        age = (datetime.fromisoformat(timestamp) - datetime.fromisoformat(saved['started_at'])).total_seconds()
        return (saved.get('fingerprint') == model['fingerprint'] and not saved.get('complete')
                and 0 <= age <= 48 * 3600 and isinstance(saved.get('run_id'), str))
    except (KeyError, TypeError, ValueError):
        return False


def qualification_run_id(candidates):
    batch = hashlib.sha256('\n'.join(sorted(m['id'] for m in candidates)).encode()).hexdigest()[:12]
    return os.environ.get('GITHUB_RUN_ID', str(time.time_ns())) + '-' + os.environ.get('GITHUB_RUN_ATTEMPT', '1') + '-' + batch


def select_candidates(candidates, previous, maximum, timestamp, progress=None):
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
        saved = (progress or {}).get('models', {}).get(model['id'], {})
        resume = resumable(saved, model, timestamp) and bool(saved['tasks'])
        return (0 if overdue else 1 if resume else 2 if followup else 3, evaluated)
    return sorted(ordered, key=priority)[:maximum]


def checkpoint_signature():
    paths = [ROOT / 'bench/tasks.py', ROOT / 'bench/Dockerfile', *sorted((ROOT / 'src').glob('*.rs'))]
    return hashlib.sha256(b''.join(p.read_bytes() for p in paths)).hexdigest()


def load_progress(path, signature):
    try:
        data = json.loads(path.read_text())
        # v0.7.3's first evaluator rejected valid shell quoting and PATHs built
        # with variables before running its behavioral checks. Those stricter
        # passes remain valid; only affected noncritical failures need rescoring.
        if (signature == 'f4e15608de42906a8769225205a68cc8be25f4a1c3ec6fcba260989b6fdc3efa'
                and data.get('signature') == '48902c2eab4a5881427ef7a2e14973ebfa10c7d468a2c8ef6186a2224f46fc42'
                and isinstance(data.get('models'), dict)):
            for saved in data['models'].values():
                if not isinstance(saved, dict) or saved.get('complete'):
                    continue  # Published suites always receive a fresh full run.
                tasks = saved.get('tasks', {}) if isinstance(saved, dict) else {}
                if not isinstance(tasks, dict):
                    continue
                for task in ('path', 'shell'):
                    result = tasks.get(task, {})
                    if isinstance(result, dict) and result.get('passed') is False and result.get('critical') is False:
                        del tasks[task]
            data['signature'] = signature
        if data.get('signature') == signature and isinstance(data.get('models'), dict):
            if data.get('outcome_scoring', 1) < OUTCOME_SCORING:
                for saved in data['models'].values():
                    if not isinstance(saved, dict) or not isinstance(saved.get('tasks'), dict):
                        continue
                    # Earlier reports omitted the stopping reason. Re-evaluate
                    # ambiguous noncritical failures once under the fixed rule;
                    # retain stricter passes and explicit protocol failures.
                    removed = False
                    for task, result in list(saved['tasks'].items()):
                        if (isinstance(result, dict) and result.get('passed') is False
                                and result.get('critical') is False and task != 'handoff'
                                and result.get('exit_code') in (None, 1)
                                and not (result.get('last_error') or {}).get('code') in {'invalid_tool_response', 'request_failed'}):
                            del saved['tasks'][task]
                            removed = True
                    if removed:
                        saved['corrects_published_run'] = bool(saved.get('complete'))
                        saved['complete'] = False
                data['outcome_scoring'] = OUTCOME_SCORING
            return data
    except (OSError, ValueError, AttributeError):
        pass
    return {'signature': signature, 'outcome_scoring': OUTCOME_SCORING, 'models': {}}


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(data, indent=2) + '\n')
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="https://api.bailout.dev")
    parser.add_argument("--output", default="artifacts/rankings.json")
    parser.add_argument("--max-requests", type=int, default=80, help="Maximum inference API submissions per candidate, including retries")
    parser.add_argument("--max-models", type=int, default=2)
    parser.add_argument("--models", nargs="*")
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--checkpoint", help="Resume completed tasks after a provider outage; never retry a scored failure")
    parser.add_argument("--diagnose-task", choices=TASKS, help="Inspect one synthetic task without publishing or changing qualification evidence")
    args = parser.parse_args()
    if not args.api.startswith("https://") or not 1 <= args.max_requests <= 160 or not 1 <= args.max_models <= 10:
        parser.error("HTTPS and bounded evaluation limits required")
    token = os.environ.get("BAILOUT_BENCHMARK_TOKEN", "")
    if len(token) < 32:
        parser.error("BAILOUT_BENCHMARK_TOKEN must be provisioned")
    if not args.skip_build:
        subprocess.run(["docker", "build", "-t", IMAGE, "-f", "bench/Dockerfile", "."], cwd=ROOT, check=True)
    catalog = discover_candidates(args.api, token, args.models)
    previous = {m["id"]: m for m in catalog["snapshot"]["models"]}
    candidates = catalog["candidates"]
    if args.diagnose_task:
        selected = [m for m in candidates if m['id'] in (args.models or [])]
        if len(selected) != 1:
            parser.error('Diagnostics require exactly one discovered --models candidate')
        meter = {'requests': 0, 'max': args.max_requests, 'lock': threading.Lock(), 'diagnostic': True}
        result = run_task(args.api, token, selected[0], args.diagnose_task, 42, meter)
        print(json.dumps({'diagnostic_task': args.diagnose_task, 'model': selected[0]['id'], **result}), flush=True)
        return  # Never score, checkpoint, or publish a diagnostic reproduction.
    timestamp = datetime.now(timezone.utc).isoformat()
    progress_path = Path(args.checkpoint) if args.checkpoint else None
    signature = checkpoint_signature()
    progress = load_progress(progress_path, signature) if progress_path else {'signature': signature, 'outcome_scoring': OUTCOME_SCORING, 'models': {}}
    if args.models:
        by_id = {m['id']: m for m in candidates}
        candidates = [by_id[id] for id in dict.fromkeys(args.models) if id in by_id][:args.max_models]
    else:
        candidates = select_candidates(candidates, previous, args.max_models, timestamp, progress)
    run_id = qualification_run_id(candidates)
    deadline = time.monotonic() + 45 * 60
    output = []
    reports = []
    target = Path(args.output)
    target.unlink(missing_ok=True)  # Never publish an artifact left over from an earlier run.
    for model in candidates:
        # A throttled first provider must not consume the next candidate's
        # entire evaluation allowance. The gateway's daily cap is still shared.
        meter = {"requests": 0, "max": args.max_requests, "lock": threading.Lock()}
        saved = progress['models'].get(model['id'], {})
        if not resumable(saved, model, timestamp):
            saved = {'fingerprint': model['fingerprint'], 'started_at': timestamp, 'run_id': run_id, 'tasks': {}}
        progress['models'][model['id']] = saved
        results = []
        for task in TASKS:
            if task in saved['tasks']:
                result = saved['tasks'][task]
                if (all(type(result.get(k)) is bool for k in ('passed', 'critical', 'native_tools', 'inconclusive'))
                        and not result['inconclusive']):
                    results.append(result)
                    print(json.dumps({'model': model['id'], 'task': task, 'resumed': True, **result}), flush=True)
                    continue
            # Allow seven minutes for the model plus a 30-second local check.
            # Stop new tasks at 45 minutes so the 60-minute job can upload evidence.
            if meter["requests"] >= meter["max"] or time.monotonic() >= deadline:
                break
            seed = int.from_bytes(hashlib.sha256((saved['run_id'] + model["id"] + task).encode()).digest()[:4])
            result = run_task(args.api, token, model, task, seed, meter)
            results.append(result)
            print(json.dumps({"model": model["id"], "task": task, **result,
                              "requests_used": meter["requests"], "request_limit": meter["max"]}), flush=True)
            if not result['inconclusive'] or result['critical']:
                saved['tasks'][task] = {**result, 'inconclusive': False}
            if progress_path:
                save_json(progress_path, progress)
            if result["inconclusive"]:
                break
        # A corrected published suite replaces its old score; its retained
        # passes are not evidence from a second independent run.
        prior = None if saved.get('corrects_published_run') else previous.get(model['id'])
        row = accumulate(model, results, prior, datetime.now(timezone.utc).isoformat())
        if row:
            output.append(row)
            saved['complete'] = len(results) == len(TASKS) and not any(r['inconclusive'] for r in results)
        reports.append({'id': model['id'], 'fingerprint': model['fingerprint'], 'tasks': dict(zip(TASKS, results)),
                        'requests_used': meter['requests'], 'complete': saved.get('complete', False)})
        if progress_path:
            save_json(progress_path, progress)
        save_json(target.with_name('qualification-report.json'), {'generated_at': datetime.now(timezone.utc).isoformat(), 'models': reports})
        if output:
            save_json(target, {"schema": 1, "suite": SUITE, "generated_at": datetime.now(timezone.utc).isoformat(), "run_id": run_id,
                "harness_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(), "models": output})
    if not output:
        raise SystemExit("No complete qualification results. Last valid production ranking is unchanged.")


if __name__ == "__main__":
    main()
