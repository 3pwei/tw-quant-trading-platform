#!/usr/bin/env python3
"""Short, read-only Linux sampler. Records are diagnostic, never acceptance gates."""
import os
from pathlib import Path
import re
import select
import subprocess
import sys
import time

UNKNOWN = 'unknown'
MAX_SAMPLES = 26
MAX_BYTES = 65536
SAMPLE_SECONDS = 5
DURATION_SECONDS = 125
NUMBER = r'(?:[0-9]{1,20}|unknown)'
FIELDS = {
    'probe': ('host_pid', 'container_pid', 'start_ticks', 'user_ticks', 'system_ticks',
              'rchar', 'read_bytes', 'syscr', 'wchar', 'write_bytes', 'syscw'),
    'host': ('cpu_total_ticks', 'cpu_idle_ticks', 'cpu_iowait_ticks', 'cpu_steal_ticks',
             'cpu_pressure_us', 'io_pressure_us', 'disk_read_sectors', 'disk_write_sectors', 'disk_io_ms'),
    'cgroup': ('usage_usec', 'nr_throttled', 'throttled_usec', 'read_bytes', 'write_bytes',
               'cpu_pressure_us', 'io_pressure_us'),
    'limits': ('clock_ticks_per_second', 'quota_us', 'period_us', 'cpuset_cpu_count', 'memory_bytes', 'pids_max',
               'read_bps', 'write_bps', 'read_iops', 'write_iops'),
}
PATTERNS = {}
for _scope, _fields in FIELDS.items():
    _suffix = r' state=(?:R|S|D|T|t|Z|X|I|unknown)' if _scope == 'probe' else ''
    PATTERNS[_scope] = re.compile(r'P8_RESOURCE scope=' + _scope +
        r' sample=(?:[0-9]|1[0-9]|2[0-5]) elapsed_ms=[0-9]{1,9}' +
        ''.join(' ' + key + '=' + NUMBER for key in _fields) + _suffix + r'\n')
STATUS = re.compile(r'P8_RESOURCE status=(?:unavailable|complete|stopped) samples=(?:[0-9]|1[0-9]|2[0-6])\n')
IDENTITY = re.compile(rb'P8_PROBE_ID pid=([1-9][0-9]{0,9}) start_ticks=([0-9]{1,20})\n')


def safe_line(line):
    return len(line) <= 768 and (STATUS.fullmatch(line) is not None or
        any(pattern.fullmatch(line) is not None for pattern in PATTERNS.values()))


def number(value):
    return value if type(value) is int and 0 <= value < 10**20 else UNKNOWN


def read(path, limit=65536):
    with Path(path).open() as stream:
        value = stream.read(limit + 1)
    if len(value) > limit:
        raise ValueError('oversize')
    return value


def optional(action):
    try:
        return action()
    except (OSError, ValueError, IndexError, KeyError, TypeError):
        return UNKNOWN


def stat(pid, proc=Path('/proc')):
    raw = read(proc / str(pid) / 'stat', 4096)
    fields = raw.rsplit(')', 1)[1].split()
    return dict(state=fields[0], user_ticks=int(fields[11]), system_ticks=int(fields[12]),
                start_ticks=int(fields[19]))


def pairs(path):
    result = {}
    for line in read(path).splitlines():
        key, value = line.replace(':', '').split()[:2]
        if value.isdecimal():
            result[key] = int(value)
    return result


def pressure(path):
    for line in read(path, 4096).splitlines():
        if line.startswith('some '):
            return int(dict(item.split('=') for item in line.split()[1:])['total'])
    raise ValueError('missing pressure')


def identity(fd):
    # pread does not disturb the collector's file offset or probe stderr writes.
    lines = os.pread(fd, 8192, 0).splitlines(keepends=True)
    matches = [IDENTITY.fullmatch(line) for line in lines]
    values = [tuple(map(int, match.groups())) for match in matches if match]
    return values[0] if len(values) == 1 else None


def matching_pid(container_pid, start_ticks, namespace, proc=Path('/proc')):
    # Bind all three identities; never guess by process name or command line.
    found = []
    for entry in proc.iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            if os.stat(entry / 'ns/pid').st_ino != namespace:
                continue
            nspid = next(line for line in read(entry / 'status').splitlines() if line.startswith('NSpid:'))
            if int(nspid.split()[-1]) == container_pid and stat(entry.name, proc)['start_ticks'] == start_ticks:
                found.append(int(entry.name))
        except (OSError, ValueError, IndexError, StopIteration):
            continue
    return found[0] if len(found) == 1 else None


def probe_sample(pid, expected, namespace, proc=Path('/proc')):
    state = stat(pid, proc)
    if state['start_ticks'] != expected[1] or os.stat(proc / str(pid) / 'ns/pid').st_ino != namespace:
        raise ValueError('process changed')
    io = optional(lambda: pairs(proc / str(pid) / 'io'))
    values = {key: io.get(key, UNKNOWN) if isinstance(io, dict) else UNKNOWN
              for key in ('rchar', 'read_bytes', 'syscr', 'wchar', 'write_bytes', 'syscw')}
    # Recheck after reads so PID reuse cannot attribute another process's counters.
    if stat(pid, proc)['start_ticks'] != expected[1]:
        raise ValueError('process changed')
    return dict(values, host_pid=pid, container_pid=expected[0], **state)


def host_sample(device, proc=Path('/proc')):
    result = {}
    cpu = optional(lambda: [int(v) for v in read(proc / 'stat').splitlines()[0].split()[1:9]])
    if isinstance(cpu, list) and len(cpu) == 8:
        result.update(cpu_total_ticks=sum(cpu), cpu_idle_ticks=cpu[3],
                      cpu_iowait_ticks=cpu[4], cpu_steal_ticks=cpu[7])
    for resource in ('cpu', 'io'):
        result[resource + '_pressure_us'] = optional(lambda: pressure(proc / 'pressure' / resource))
    if device is not None:
        def disk():
            rows = [row.split() for row in read(proc / 'diskstats').splitlines()]
            row = next(r for r in rows if (int(r[0]), int(r[1])) == device)
            return dict(disk_read_sectors=int(row[5]), disk_write_sectors=int(row[9]), disk_io_ms=int(row[12]))
        try:
            result.update(disk())
        except (OSError, ValueError, IndexError, StopIteration):
            pass
    return result


def cgroup_path(init_pid, proc=Path('/proc'), root=Path('/sys/fs/cgroup')):
    line = next(row for row in read(proc / str(init_pid) / 'cgroup').splitlines() if row.startswith('0::'))
    relative = line[3:]
    if not relative.startswith('/') or '..' in Path(relative).parts:
        raise ValueError('invalid cgroup')
    path = (root / relative.lstrip('/')).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError('outside cgroup')
    return path


def io_values(path, device):
    if device is None:
        return {}
    key = str(device[0]) + ':' + str(device[1])
    rows = [line.split() for line in read(path).splitlines()]
    row = next((line for line in rows if line[0] == key), [])
    return dict(item.split('=', 1) for item in row[1:])


def limit_value(value):
    # Zero is the explicit unlimited sentinel; unknown means absent/unreadable.
    return 0 if value == 'max' else int(value)


def cpu_count(value):
    if re.fullmatch(r'[0-9]+(?:-[0-9]+)?(?:,[0-9]+(?:-[0-9]+)?)*', value) is None:
        raise ValueError('invalid cpuset')
    cpus = set()
    for item in value.split(','):
        bounds = [int(n) for n in item.split('-')]
        first, last = bounds[0], bounds[-1]
        if not 0 <= first <= last <= 65535 or last - first > 4096:
            raise ValueError('cpuset bounds')
        cpus.update(range(first, last + 1))
    return len(cpus)


def cgroup_limits(path, device):
    if path is None:
        return {}
    values = {}
    values['cpuset_cpu_count'] = optional(lambda: cpu_count(read(path / 'cpuset.cpus.effective', 4096).strip()))
    cpu = optional(lambda: read(path / 'cpu.max', 128).split())
    if isinstance(cpu, list) and len(cpu) == 2:
        values['quota_us'] = optional(lambda: limit_value(cpu[0]))
        values['period_us'] = optional(lambda: int(cpu[1]))
    for key, filename in (('memory_bytes', 'memory.max'), ('pids_max', 'pids.max')):
        values[key] = optional(lambda: limit_value(read(path / filename, 128).strip()))
    io = optional(lambda: io_values(path / 'io.max', device))
    if isinstance(io, dict):
        for key, field in (('read_bps', 'rbps'), ('write_bps', 'wbps'), ('read_iops', 'riops'), ('write_iops', 'wiops')):
            values[key] = optional(lambda: limit_value(io.get(field, 'max'))) if device else UNKNOWN
    return values


def cgroup_sample(path, device):
    if path is None:
        return {}
    values = optional(lambda: pairs(path / 'cpu.stat'))
    values = values if isinstance(values, dict) else {}
    for resource in ('cpu', 'io'):
        values[resource + '_pressure_us'] = optional(lambda: pressure(path / (resource + '.pressure')))
    io = optional(lambda: io_values(path / 'io.stat', device))
    if isinstance(io, dict):
        for key, field in (('read_bytes', 'rbytes'), ('write_bytes', 'wbytes')):
            values[key] = optional(lambda: int(io[field]))
    return values


def emit(scope, index, elapsed, values):
    line = 'P8_RESOURCE scope=' + scope + ' sample=' + str(index) + ' elapsed_ms=' + str(elapsed)
    line += ''.join(' ' + key + '=' + str(number(values.get(key))) for key in FIELDS[scope])
    if scope == 'probe':
        state = values.get('state')
        line += ' state=' + (state if state in ('R', 'S', 'D', 'T', 't', 'Z', 'X', 'I') else UNKNOWN)
    if safe_line(line + '\n'):
        print(line, flush=True)


def sample(cid, diagnostic_fd, stop_fd, started_ns):
    # Same host monotonic clock as the collector, including observer startup.
    started = started_ns / 1_000_000_000
    if re.fullmatch('[0-9a-f]{64}', cid) is None:
        raise ValueError('invalid container')
    # Only fixed metadata; no environment, command line, names or raw inspect dump.
    result = subprocess.run(['docker', 'inspect', '--format', '{{json .State.Pid}}', cid],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=1, check=True)
    init_pid = int(result.stdout)
    if init_pid <= 0:
        raise ValueError('container not running')
    initial = optional(lambda: stat(init_pid)['start_ticks'])
    namespace = optional(lambda: os.stat('/proc/' + str(init_pid) + '/ns/pid').st_ino) if type(initial) is int else UNKNOWN
    try:
        group = cgroup_path(init_pid)
    except (OSError, ValueError, StopIteration):
        group = None
    device = optional(lambda: os.stat('/proc/' + str(init_pid) + '/root/data/staging.sqlite3').st_dev)
    device = (os.major(device), os.minor(device)) if type(device) is int else None
    limits = cgroup_limits(group, device)
    limits['clock_ticks_per_second'] = optional(lambda: os.sysconf('SC_CLK_TCK'))
    emit('limits', 0, int((time.monotonic() - started) * 1000), limits)
    bound, host_pid, count = None, None, 0
    for index in range(MAX_SAMPLES):
        elapsed = time.monotonic() - started
        if elapsed >= DURATION_SECONDS:
            break
        if type(initial) is int and optional(lambda: stat(init_pid)['start_ticks']) != initial:
            break
        if bound is None:
            bound = identity(diagnostic_fd)
            if bound is not None and type(namespace) is int:
                host_pid = matching_pid(*bound, namespace)
        values = optional(lambda: probe_sample(host_pid, bound, namespace)) if host_pid else {}
        emit('probe', index, int(elapsed * 1000), values if isinstance(values, dict) else {})
        emit('host', index, int(elapsed * 1000), host_sample(device))
        emit('cgroup', index, int(elapsed * 1000), cgroup_sample(group, device))
        count += 1
        remaining = min(SAMPLE_SECONDS, DURATION_SECONDS - (time.monotonic() - started))
        if remaining <= 0:
            break
        if select.select([stop_fd], [], [], remaining)[0]:
            print('P8_RESOURCE status=stopped samples=' + str(count), flush=True)
            return
    print('P8_RESOURCE status=complete samples=' + str(count), flush=True)


if __name__ == '__main__':
    try:
        sample(sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]))
    except Exception:
        print('P8_RESOURCE status=unavailable samples=0', flush=True)
