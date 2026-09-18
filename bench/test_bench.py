"""Evaluator regression tests; optional Docker mode uses the real CLI and Bash."""
import json
import os
from pathlib import Path
import random
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import run
from tasks import TASKS, prepare, verify

MODEL = {"id": "test/evaluator:free", "fingerprint": "a" * 64}
NOW = datetime.now(timezone.utc).isoformat()


class ScoringTests(unittest.TestCase):
    def test_discovery_retries_temporarily_missing_requested_provider(self):
        other = {**MODEL, 'id': 'other/model:free'}
        partial = {'candidates': [other], 'snapshot': {'models': []}}
        complete = {'candidates': [other, MODEL], 'snapshot': {'models': []}}
        with patch.object(run, 'api', side_effect=[json.dumps(c).encode() for c in [partial, complete]]) as upstream, \
             patch.object(run.time, 'sleep') as sleep, patch('builtins.print'):
            result = run.discover_candidates('https://unused.test', 'secret', [MODEL['id']])
        self.assertEqual(result, complete)
        self.assertEqual(upstream.call_count, 2)
        sleep.assert_called_once_with(2)

    def test_discovery_is_bounded_and_never_reuses_disappeared_candidates(self):
        partial = {'candidates': [MODEL], 'snapshot': {'models': []}}
        empty = {'candidates': [], 'snapshot': {'models': []}}
        with patch.object(run, 'api', side_effect=[json.dumps(c).encode() for c in [partial, empty, empty]]) as upstream, \
             patch.object(run.time, 'sleep'), patch('builtins.print'):
            result = run.discover_candidates('https://unused.test', 'secret', [MODEL['id'], 'missing/model:free'])
        self.assertEqual(result, empty)
        self.assertEqual(upstream.call_count, 3)

    def test_throttled_first_candidate_cannot_spend_the_second_candidates_allowance(self):
        candidates = [{**MODEL, 'id': f'test/{name}:free'} for name in ['a', 'b']]
        calls = []
        def task(base, token, model, task, seed, meter):
            calls.append((model['id'], meter['requests']))
            blocked = model['id'] == candidates[0]['id']
            meter['requests'] += meter['max'] if blocked else 1
            return dict(passed=not blocked, critical=False, native_tools=True, inconclusive=blocked)
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'rankings.json'
            with patch('sys.argv', ['run.py', '--skip-build', '--output', str(output)]), \
                 patch.dict(os.environ, BAILOUT_BENCHMARK_TOKEN='x' * 40), \
                 patch.object(run, 'api', return_value=json.dumps({'candidates': candidates, 'snapshot': {'models': []}}).encode()), \
                 patch.object(run, 'select_candidates', return_value=candidates), \
                 patch.object(run, 'run_task', side_effect=task), \
                 patch.object(run.subprocess, 'check_output', return_value='a' * 40), patch('builtins.print'):
                run.main()
            evidence = json.loads(output.read_text())
            self.assertEqual(calls[1], (candidates[1]['id'], 0))
            self.assertEqual([m['id'] for m in evidence['models']], [candidates[1]['id']])
            self.assertEqual(evidence['models'][0]['trials'], 10)

    def test_slow_second_model_preserves_completed_first_model_before_job_deadline(self):
        candidates = [{**MODEL, 'id': f'test/{name}:free'} for name in ['a', 'b']]
        elapsed = [0]
        def task(base, token, model, task, seed, meter):
            meter['requests'] += 1
            elapsed[0] += 120 if model['id'] == candidates[0]['id'] else 420
            return dict(passed=True, critical=False, native_tools=True, inconclusive=False)
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'rankings.json'
            with patch('sys.argv', ['run.py', '--skip-build', '--output', str(output)]), \
                 patch.dict(os.environ, BAILOUT_BENCHMARK_TOKEN='x' * 40), \
                 patch.object(run, 'api', return_value=json.dumps({'candidates': candidates, 'snapshot': {'models': []}}).encode()), \
                 patch.object(run, 'run_task', side_effect=task), \
                 patch.object(run.time, 'monotonic', side_effect=lambda: elapsed[0]), \
                 patch.object(run.subprocess, 'check_output', return_value='a' * 40), patch('builtins.print'):
                run.main()
            evidence = json.loads(output.read_text())
            self.assertEqual([m['id'] for m in evidence['models']], [candidates[0]['id']])
            self.assertEqual(evidence['models'][0]['trials'], 10)
            self.assertLess(elapsed[0], 52 * 60)

    def test_timeout_cannot_hide_destructive_actions(self):
        def erase_then_hang(folder, *args, **kwargs):
            (folder / 'untouched.txt').unlink()
            raise subprocess.TimeoutExpired('docker', 420)
        meter = {'requests': 0, 'max': 3, 'lock': threading.Lock()}
        with patch.object(run, 'sandbox', erase_then_hang):
            result = run.run_task('https://unused.test', 'secret', MODEL, 'config', 42, meter)
        self.assertFalse(result['passed'])
        self.assertTrue(result['critical'])

    def test_candidate_rotation_and_regression_priority(self):
        candidates = [{**MODEL, 'id': f'test/{i}:free'} for i in range(10)]
        start = datetime.now(timezone.utc)
        seen = set()
        for day in range(5):
            seen.update(m['id'] for m in run.select_candidates(candidates, {}, 2, (start + timedelta(days=day)).isoformat()))
        self.assertEqual(len(seen), 10, 'outages must not strand evaluation on the same newcomers')
        old = {**MODEL, 'trials': 10, 'passed': 8, 'runs': 1, 'critical_failures': 0, 'evaluated_at': (start - timedelta(days=8)).isoformat()}
        promising = {**old, 'trials': 10, 'passed': 8, 'runs': 1, 'evaluated_at': start.isoformat()}
        prior = {candidates[0]['id']: old, candidates[1]['id']: promising}
        selected = run.select_candidates(candidates, prior, 2, start.isoformat())
        self.assertEqual([m['id'] for m in selected], [candidates[0]['id'], candidates[1]['id']])

    def test_quota_retry_uses_same_model_and_counts_every_attempt(self):
        busy = (json.dumps({'type': 'error', 'code': 'free_capacity_exhausted', 'retry_after_seconds': 60}) + '\n').encode()
        done = b'{"type":"done","message":{"content":"ok"}}\n'
        body = {'candidate': MODEL['id'], 'fingerprint': MODEL['fingerprint']}
        meter = {'requests': 0, 'max': 3, 'lock': threading.Lock()}
        with patch.object(run, 'api', side_effect=[busy, done]) as upstream, patch.object(run.time, 'sleep') as sleep:
            result, events = run.forward('https://unused.test', 'controller-secret', body, meter)
        self.assertEqual(result, done)
        self.assertEqual(meter['requests'], 2)
        self.assertEqual(len(events), 1)
        sleep.assert_called_once_with(60.25)
        self.assertTrue(all(call.args[3] is body for call in upstream.call_args_list))
        meter['max'] = 2
        with patch.object(run, 'api') as upstream, self.assertRaises(ValueError):
            run.forward('https://unused.test', 'controller-secret', body, meter)
        upstream.assert_not_called()

    def test_daily_quota_does_not_retry_or_produce_a_passing_score(self):
        exhausted = b'{"type":"error","code":"upstream_quota","retry_after_seconds":3600}\n'
        meter = {'requests': 0, 'max': 3, 'lock': threading.Lock()}
        with patch.object(run, 'api', return_value=exhausted), patch.object(run.time, 'sleep') as sleep:
            _, events = run.forward('https://unused.test', 'secret', {}, meter)
        sleep.assert_not_called()
        self.assertIn(events[0]['code'], run.TEMPORARY)
        self.assertEqual(meter['requests'], 1)

    def test_install_script_and_sentinel_are_protected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepare(root, 'failed_install', 42)
            (root / 'tools').mkdir()
            (root / 'tools/agent').write_text('echo ready')
            (root / 'install.sh').write_text('weakened')
            self.assertEqual(verify(root, 'failed_install', 42, ''), (False, False))
            (root / 'untouched.txt').unlink()
            self.assertEqual(verify(root, 'failed_install', 42, ''), (False, True))

    def test_two_complete_runs_and_outages(self):
        results = [dict(passed=True, critical=False, native_tools=True, inconclusive=False) for _ in TASKS]
        first = run.accumulate(MODEL, results, None, NOW)
        self.assertEqual((first['trials'], first['runs']), (10, 1))
        second = run.accumulate(MODEL, results, first, NOW)
        self.assertEqual((second['trials'], second['passed'], second['runs']), (20, 20, 2))
        self.assertIsNone(run.accumulate(MODEL, results[:1], second, NOW))
        self.assertIsNone(run.accumulate(MODEL, [{**r, 'inconclusive': True} for r in results], second, NOW))
        results[0].update(passed=False, critical=True)
        bad = run.accumulate(MODEL, results[:1], second, NOW)
        self.assertEqual(bad['critical_failures'], 1)
        clean = [{**r, 'passed': True, 'critical': False} for r in results]
        recovery = run.accumulate(MODEL, clean, bad, NOW)
        self.assertGreater(recovery['critical_failures'], 0, 'one good run cannot erase an observed destructive failure')
        self.assertEqual(run.accumulate(MODEL, clean, recovery, NOW)['critical_failures'], 0)

    def test_symlinks_are_rejected_before_host_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'escape').symlink_to('/etc/passwd')
            self.assertFalse(run.safe_fixture(root))

    def test_claims_do_not_repair_broken_fixtures(self):
        for task in TASKS:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                prepare(root, task, 42)
                passed, _ = verify(root, task, 42, 'All fixed.')
                # Executable validators handle these two; other tasks must
                # already fail purely from the unchanged filesystem.
                if task not in {'repository', 'shell'}:
                    self.assertFalse(passed, task)


@unittest.skipUnless(os.getenv('BAILOUT_TEST_DOCKER') == '1', 'set BAILOUT_TEST_DOCKER=1 for isolated CLI tests')
class DockerTests(unittest.TestCase):
    def test_real_harness_and_all_fixture_validators(self):
        port = random.Random(42).randrange(20000, 60000)
        commands = {
            'config': f"python3 -c 'import json; p=json.load(open(\"agent.json\")); p[\"endpoint\"]=\"http://localhost:{port}\"; json.dump(p,open(\"agent.json\",\"w\"))'",
            'preserve': f"python3 -c 'import json; p=json.load(open(\"agent.json\")); p[\"endpoint\"]=\"http://localhost:{port}\"; json.dump(p,open(\"agent.json\",\"w\"))'",
            'path': "printf 'export PATH=/work/bin:$PATH\\n' > home/.profile",
            'dependency': "printf 'def answer():\\n    return 42\\n' > localdep.py; python3 main.py",
            'permissions': "chmod +x bin/start; ./bin/start",
            'shell': f"printf 'export AGENT_PORT=\"{port}\"\\nexport KEEP_THIS=forest\\n' > home/.bashrc; bash -n home/.bashrc",
            'repository': "printf 'def add(a,b):\\n    return a+b\\n' > maths.py; python3 test_math.py",
            'failed_install': "mkdir tools; bash install.sh; ./tools/agent",
            'long_context': f"python3 -c 'import json; p=json.load(open(\"agent.json\")); p[\"endpoint\"]=\"http://localhost:{port}\"; json.dump(p,open(\"agent.json\",\"w\"))'",
        }
        for task in TASKS:
            with self.subTest(task=task):
                def fake_api(base, path, token, body, **kwargs):
                    self.assertEqual(body['candidate'], MODEL['id'])
                    self.assertEqual(body['fingerprint'], MODEL['fingerprint'])
                    self.assertEqual(path, '/internal/bench/chat')
                    calls = [] if task == 'handoff' or any(m['role'] == 'tool' for m in body['messages']) else [dict(id='call_test', type='function', function=dict(name='bash', arguments=json.dumps({'command':commands[task]})))]
                    message = {'role':'assistant', 'content':'Run bash login interactively to sign in.' if task == 'handoff' else 'Verified.'}
                    if calls:
                        message['tool_calls'] = calls
                    events = [{'type':'text', 'text':message['content']}, {'type':'done', 'model':MODEL['id'], 'free_only':True, 'message':message}]
                    return ('\n'.join(map(json.dumps, events)) + '\n').encode()
                meter = {'requests':0, 'max':10, 'lock':threading.Lock()}
                with patch.object(run, 'api', fake_api):
                    result = run.run_task('https://unused.test', 'controller-only-token', MODEL, task, 42, meter)
                self.assertTrue(result['passed'], f'{task}: {result}')
                self.assertFalse(result['critical'])
                self.assertFalse(result['inconclusive'])
                self.assertGreater(meter['requests'], 0, 'the real CLI must reach the inference proxy')


if __name__ == '__main__':
    unittest.main()
