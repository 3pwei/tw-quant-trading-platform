"""Linux sampling identity, counter interpretation, output bounds and real cleanup."""
from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

STAGING = Path(__file__).resolve().parents[1] / 'deploy/staging'


def load(name):
    spec = importlib.util.spec_from_file_location(name, STAGING / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sampling = load('resource_sampling')
evidence = load('acceptance_evidence')
diagnostic = load('readonly_diagnostics')


def proc_stat(pid, start, user=7, system=3):
    fields = ['0'] * 40
    fields[0], fields[11], fields[12], fields[19] = 'S', str(user), str(system), str(start)
    return str(pid) + ' (a tricky ) name) ' + ' '.join(fields)


class IdentityAndCounterTests(unittest.TestCase):
    def fixture(self, root, pid, container_pid, start, namespace_source=None):
        entry = root / str(pid)
        (entry / 'ns').mkdir(parents=True)
        (entry / 'stat').write_text(proc_stat(pid, start))
        (entry / 'status').write_text('Name:\tprivate-name\nNSpid:\t' + str(pid) + '\t' + str(container_pid) + '\n')
        (entry / 'io').write_text('rchar: 1024\nread_bytes: 512\nsyscr: 2\nwchar: 20\nwrite_bytes: 0\nsyscw: 1\n')
        if namespace_source:
            os.link(namespace_source, entry / 'ns/pid')
        else:
            (entry / 'ns/pid').touch()
        return entry

    def test_pid_namespace_starttime_and_ambiguity_all_bind_before_reading(self):
        with tempfile.TemporaryDirectory() as directory:
            proc = Path(directory)
            right = self.fixture(proc, 123, 7, 100)
            ns = (right / 'ns/pid').stat().st_ino
            self.fixture(proc, 124, 7, 100)  # same inner PID, different namespace
            wrong_start = self.fixture(proc, 125, 7, 200, right / 'ns/pid')
            self.assertEqual(sampling.matching_pid(7, 100, ns, proc), 123)
            values = sampling.probe_sample(123, (7, 100), ns, proc)
            self.assertEqual((values['user_ticks'], values['system_ticks'], values['read_bytes']), (7, 3, 512))
            (right / 'stat').write_text(proc_stat(123, 101))
            with self.assertRaises(ValueError):
                sampling.probe_sample(123, (7, 100), ns, proc)
            self.assertIsNone(sampling.matching_pid(7, 100, ns, proc))
            (right / 'stat').write_text(proc_stat(123, 100))
            (wrong_start / 'stat').write_text(proc_stat(125, 100))
            self.assertIsNone(sampling.matching_pid(7, 100, ns, proc))

    def test_handshake_is_unique_bounded_and_does_not_move_stream_offset(self):
        marker = b'P8_PROBE_ID pid=7 start_ticks=100\n'
        with tempfile.TemporaryFile() as stream:
            stream.write(b'credential=never-log\n' + marker)
            stream.flush()
            offset = stream.tell()
            self.assertEqual(sampling.identity(stream.fileno()), (7, 100))
            self.assertEqual(stream.tell(), offset)
            stream.write(marker)
            stream.flush()
            self.assertIsNone(sampling.identity(stream.fileno()))

    def test_host_totals_exclude_guest_and_disk_is_only_selected_device(self):
        with tempfile.TemporaryDirectory() as directory:
            proc = Path(directory)
            (proc / 'stat').write_text('cpu 10 1 2 100 3 4 5 6 900 800\n')
            (proc / 'pressure').mkdir()
            for name in ('cpu', 'io'):
                (proc / 'pressure' / name).write_text('some avg10=2.0 avg60=0 avg300=0 total=123\n')
            (proc / 'diskstats').write_text('8 1 private-name 1 2 30 4 5 6 70 8 9 10 11\n8 2 other 1 2 999 4 5 6 999 8 9 999 11\n')
            values = sampling.host_sample((8, 1), proc)
            self.assertEqual(values['cpu_total_ticks'], 131)
            self.assertEqual(values['disk_read_sectors'], 30)
            self.assertEqual(values['disk_write_sectors'], 70)
            self.assertEqual(values['disk_io_ms'], 10)
            self.assertEqual(values['io_pressure_us'], 123)
            (proc / 'pressure/io').unlink()
            self.assertEqual(sampling.host_sample((8, 1), proc)['io_pressure_us'], 'unknown')

    def test_cgroup_limits_counters_missing_files_and_path_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            group = root / 'cgroup'; group.mkdir()
            (group / 'cpu.max').write_text('20000 100000\n')
            (group / 'cpuset.cpus.effective').write_text('0-1,4\n')
            (group / 'memory.max').write_text('max\n')
            (group / 'pids.max').write_text('100\n')
            (group / 'io.max').write_text('8:1 rbps=1000 wbps=max riops=20 wiops=max\n')
            (group / 'cpu.stat').write_text('usage_usec 123\nnr_throttled 4\nthrottled_usec 500\n')
            (group / 'io.stat').write_text('8:1 rbytes=100 wbytes=200 rios=3 wios=4\n')
            limits = sampling.cgroup_limits(group, (8, 1))
            self.assertEqual(limits['quota_us'], 20000)
            self.assertEqual(limits['cpuset_cpu_count'], 3)
            self.assertEqual(limits['memory_bytes'], 0)
            self.assertEqual(limits['write_bps'], 0)
            self.assertEqual(limits['read_bps'], 1000)
            counters = sampling.cgroup_sample(group, (8, 1))
            self.assertEqual(counters['nr_throttled'], 4)
            self.assertEqual(counters['read_bytes'], 100)
            self.assertEqual(counters['cpu_pressure_us'], 'unknown')
            (root / 'proc/1').mkdir(parents=True)
            (root / 'proc/1/cgroup').write_text('0::/../../private\n')
            with self.assertRaises(ValueError):
                sampling.cgroup_path(1, root / 'proc', group)

    def test_output_is_fixed_numeric_allowlist_and_fits_both_transports(self):
        out = io.StringIO()
        with redirect_stdout(out):
            for scope in sampling.FIELDS:
                sampling.emit(scope, 15, 15000, {key: 'credential=never-log' for key in sampling.FIELDS[scope]})
        lines = out.getvalue().splitlines(keepends=True)
        self.assertEqual(len(lines), 4)
        self.assertTrue(all(sampling.safe_line(line) for line in lines))
        self.assertNotIn('never-log', out.getvalue())
        self.assertEqual(len(diagnostic.safe_lines(out.getvalue().encode())), 4)
        for bad in (lines[0].replace('unknown', '-1', 1), lines[0].replace('sample=15', 'sample=16'),
                    lines[0].rstrip() + ' secret=abc\n', lines[0].rstrip('\n'),
                    'P8_RESOURCE status=private samples=0\n'):
            self.assertFalse(sampling.safe_line(bad))


@unittest.skipUnless(sys.platform == 'linux', 'Linux /proc required')
class RealProcessTests(unittest.TestCase):
    def run_probe(self, duration, *, fail_inspect=False, slow_inspect=False):
        with tempfile.TemporaryDirectory() as directory:
            docker = Path(directory) / 'docker'
            docker.write_text('#!' + sys.executable + '\nimport os,sys\n'
                "if sys.argv[1] == 'inspect':\n" +
                (' import time; time.sleep(30)\n' if slow_inspect else '') +
                (" raise SystemExit(1)\n" if fail_inspect else ' print(' + str(os.getpid()) + ')\n') +
                "else:\n os.execv(sys.executable, [sys.executable, *sys.argv[5:]])\n")
            docker.chmod(0o755)
            source = '''import os,time,sys
from pathlib import Path
start = int(Path('/proc/self/stat').read_text().rsplit(')',1)[1].split()[19])
print('P8_PROBE_ID pid=' + str(os.getpid()) + ' start_ticks=' + str(start), file=sys.stderr, flush=True)
print('P8_DURABLE_STEP=START step=sqlite-integrity-execute elapsed_ms=0 category=none',file=sys.stderr,flush=True)
deadline = time.monotonic() + DURATION
while time.monotonic() < deadline:
    with open('/proc/self/stat') as stream: stream.read()
    time.sleep(0.001)
print('{}')
'''.replace('DURATION', str(duration)).encode()
            log, processes = io.StringIO(), []
            original = subprocess.Popen
            def launch(*args, **kwargs):
                process = original(*args, **kwargs)
                processes.append(process)
                return process
            started = time.monotonic()
            failure = None
            with patch.dict(os.environ, {'PATH': directory + os.pathsep + os.environ['PATH']}), \
                    patch.object(evidence.subprocess, 'Popen', side_effect=launch), redirect_stderr(log):
                try:
                    evidence.run('durable-snapshot', ['docker', 'exec', '-i', 'a' * 64, 'python', '-', 'snapshot'], data=source)
                except evidence.CollectionStepError as error:
                    failure = error
            self.assertTrue(processes)
            self.assertTrue(all(p.poll() is not None for p in processes))
            return log.getvalue(), failure, time.monotonic() - started

    def test_real_sampler_progresses_while_probe_waits_and_is_reaped_on_success(self):
        log, failure, duration = self.run_probe(2.3)
        self.assertIsNone(failure)
        rows = [line for line in log.splitlines() if line.startswith('P8_RESOURCE scope=probe') and 'host_pid=unknown' not in line]
        try:
            os.stat('/proc/' + str(os.getpid()) + '/ns/pid')
        except OSError:
            if os.environ.get('CI'):
                self.fail('CI must permit real namespace-bound process sampling')
            # Restricted local workspaces still exercise real lifecycle/host sampling.
            self.assertIn('P8_RESOURCE scope=host sample=2', log)
            self.assertIn('P8_EVIDENCE_STEP=PASS step=durable-snapshot', log)
            return
        self.assertGreaterEqual(len(rows), 2, log)
        counters = [dict(item.split('=', 1) for item in row.split()[1:]) for row in rows]
        self.assertGreater(int(counters[-1]['rchar']), int(counters[0]['rchar']))
        self.assertIn('P8_EVIDENCE_STEP=PASS step=durable-snapshot', log)
        self.assertNotIn('P8_PROBE_ID', log)
        self.assertLess(duration, 5)

    def test_real_15_second_timeout_retains_samples_and_reaps_sampler_without_retry(self):
        log, failure, duration = self.run_probe(30)
        self.assertEqual(failure.category, 'timeout')
        self.assertGreaterEqual(failure.elapsed_ms, 15000)
        self.assertLess(failure.elapsed_ms, 16000)
        self.assertLess(duration, 16)
        self.assertIn('P8_RESOURCE scope=probe', log)
        self.assertIn('sample=10', log)
        self.assertNotIn('P8_EVIDENCE_STEP=PASS step=durable-snapshot', log)

    def test_unavailable_sampler_does_not_change_success(self):
        log, failure, duration = self.run_probe(0.2, fail_inspect=True)
        self.assertIsNone(failure)
        self.assertIn('P8_RESOURCE status=unavailable samples=0', log)
        self.assertIn('P8_EVIDENCE_STEP=PASS step=durable-snapshot', log)

    def test_slow_metadata_lookup_is_stopped_without_delaying_probe_result(self):
        log, failure, duration = self.run_probe(0.2, slow_inspect=True)
        self.assertIsNone(failure)
        self.assertLess(duration, 2)
        self.assertIn('P8_RESOURCE status=unavailable samples=0', log)
        self.assertIn('P8_EVIDENCE_STEP=PASS step=durable-snapshot', log)


if __name__ == '__main__':
    unittest.main()
