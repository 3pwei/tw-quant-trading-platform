"""Read-only P9 approval capture; run as root via SSH stdin, never upload to host.

No file contents, credentials, container commands, network calls or writes are
performed. Linux O_NOATIME is mandatory. Hashes are emitted only after all six
files and the rollback structure pass, and metadata remains stable.
"""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

ROOT_PARTS = ('srv', 'trading-platform-production')
ROOT_ACCESS_REASONS = (
    'production-root-slash-access',
    'production-root-srv-access',
)
PLATFORM_PATH_REASONS = (
    'production-root-platform-missing',
    'production-root-platform-symlink',
    'production-root-platform-not-directory',
    'production-root-platform-permission',
    'production-root-platform-open-failed',
)
LEGACY_REVISION = '683bb4ebc4c4980480a4786136701ff458338a14'
FILES = (
    ('market.env', 'config', 'market.env', 0),
    ('execution.env', 'config', 'execution.env', 0),
    ('gateway.env', 'config', 'gateway.env', 0),
    ('factory', 'provider', 'factory', 10001),
    ('replay.csv', 'config', 'replay.csv', 10001),
    ('rollback.json', 'rollback', 'rollback.json', 0),
)
SEALED_FILES = frozenset((
    'data.sqlite3', 'config.tar', 'image-market-api.tar',
    'image-execution-worker.tar', 'image-gateway.tar',
))
REMOTE_REASONS = frozenset((
    'root-required', 'noatime-unavailable', 'invalid-root',
    *ROOT_ACCESS_REASONS, *PLATFORM_PATH_REASONS,
    'root-not-directory', 'production-root-owner-mode',
    'config-path-access', 'config-owner-mode',
    'provider-path-access', 'provider-owner-mode',
    'rollback-path-access', 'rollback-owner-mode',
    'market.env-path-access', 'market.env-not-regular-or-symlink', 'market.env-shared-file',
    'market.env-owner-mode', 'market.env-changed-before-read', 'market.env-read-access',
    'market.env-changed-during-capture',
    'execution.env-path-access', 'execution.env-not-regular-or-symlink', 'execution.env-shared-file',
    'execution.env-owner-mode', 'execution.env-changed-before-read', 'execution.env-read-access',
    'execution.env-changed-during-capture',
    'gateway.env-path-access', 'gateway.env-not-regular-or-symlink', 'gateway.env-shared-file',
    'gateway.env-owner-mode', 'gateway.env-changed-before-read', 'gateway.env-read-access',
    'gateway.env-changed-during-capture',
    'factory-path-access', 'factory-not-regular-or-symlink', 'factory-shared-file',
    'factory-owner-mode', 'factory-changed-before-read', 'factory-read-access',
    'factory-changed-during-capture',
    'replay.csv-path-access', 'replay.csv-not-regular-or-symlink', 'replay.csv-shared-file',
    'replay.csv-owner-mode', 'replay.csv-changed-before-read', 'replay.csv-read-access',
    'replay.csv-changed-during-capture',
    'rollback.json-path-access', 'rollback.json-not-regular-or-symlink', 'rollback.json-shared-file',
    'rollback.json-owner-mode', 'rollback.json-changed-before-read', 'rollback.json-read-access',
    'rollback.json-changed-during-capture', 'rollback-json-too-large', 'rollback-json-invalid',
    'rollback-duplicate-key', 'rollback-structure-invalid', 'rollback-inventory-structure-invalid',
    'directory-changed-during-capture', 'no-path-overrides-permitted', 'capture-failed',
))
RUNNER_REASONS = frozenset((
    'control-gate-failed', 'master-moved', 'ssh-config-unavailable',
    'ssh-inspection-failed', 'unsafe-host-evidence',
))


class CaptureBlocked(Exception):
    pass


def require(condition, reason):
    if not condition:
        raise CaptureBlocked(reason)


def identity(s):
    return (s.st_dev, s.st_ino, s.st_mode, s.st_uid, s.st_gid, s.st_nlink,
            s.st_size, s.st_mtime_ns, s.st_ctime_ns)


def no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'rollback-duplicate-key')
        result[key] = value
    return result


def capture(_root_parts=ROOT_PARTS):
    """The private root argument permits isolated fixture tests, not a CLI override."""
    require(os.geteuid() == 0, 'root-required')
    require(hasattr(os, 'O_NOATIME'), 'noatime-unavailable')
    dir_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NOATIME
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NOATIME | os.O_NONBLOCK
    require(all(p and p not in ('.', '..') and '/' not in p for p in _root_parts), 'invalid-root')
    directory_links = []
    opened = []
    with contextlib.ExitStack() as stack:
        def opened_fd(path, flags, reason, parent=None):
            try:
                fd = os.open(path, flags, dir_fd=parent)
            except OSError:
                raise CaptureBlocked(reason) from None
            stack.callback(os.close, fd)
            return fd

        def opened_platform_root(path, flags, parent):
            try:
                metadata = os.stat(path, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                raise CaptureBlocked(PLATFORM_PATH_REASONS[0]) from None
            except OSError:
                raise CaptureBlocked(PLATFORM_PATH_REASONS[4]) from None
            require(not stat.S_ISLNK(metadata.st_mode), PLATFORM_PATH_REASONS[1])
            require(stat.S_ISDIR(metadata.st_mode), PLATFORM_PATH_REASONS[2])
            try:
                fd = os.open(path, flags, dir_fd=parent)
            except PermissionError:
                raise CaptureBlocked(PLATFORM_PATH_REASONS[3]) from None
            except OSError:
                raise CaptureBlocked(PLATFORM_PATH_REASONS[4]) from None
            stack.callback(os.close, fd)
            return fd

        current = opened_fd('/', dir_flags, ROOT_ACCESS_REASONS[0])
        for index, component in enumerate(_root_parts):
            parent = current
            if index == len(_root_parts) - 1:
                current = opened_platform_root(component, dir_flags, parent)
            else:
                current = opened_fd(component, dir_flags, ROOT_ACCESS_REASONS[1], parent)
            s = os.fstat(current)
            require(stat.S_ISDIR(s.st_mode), 'root-not-directory')
            directory_links.append((parent, component, current, identity(s)))
        root = current
        s = os.fstat(root)
        require(s.st_uid == 0 and stat.S_IMODE(s.st_mode) == 0o700, 'production-root-owner-mode')

        parents = {}
        for directory in ('config', 'provider', 'rollback'):
            fd = opened_fd(directory, dir_flags, directory + '-path-access', root)
            s = os.fstat(fd)
            require(s.st_uid == 0 and stat.S_IMODE(s.st_mode) == 0o700, directory + '-owner-mode')
            parents[directory] = fd
            directory_links.append((root, directory, fd, identity(s)))

        # Validate all six paths before reading a single byte for hashing.
        for label, directory, name, uid in FILES:
            parent = parents[directory]
            try:
                before = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except OSError:
                raise CaptureBlocked(label + '-path-access') from None
            require(stat.S_ISREG(before.st_mode), label + '-not-regular-or-symlink')
            require(before.st_nlink == 1, label + '-shared-file')
            require(before.st_uid == uid and stat.S_IMODE(before.st_mode) in (0o400, 0o600),
                    label + '-owner-mode')
            fd = opened_fd(name, file_flags, label + '-path-access', parent)
            require(identity(os.fstat(fd)) == identity(before), label + '-changed-before-read')
            opened.append((label, parent, name, fd, identity(before)))

        hashes = {}
        rollback_bytes = bytearray()
        for label, parent, name, fd, before in opened:
            digest = hashlib.sha256()
            while True:
                try:
                    block = os.read(fd, 1024 * 1024)
                except OSError:
                    raise CaptureBlocked(label + '-read-access') from None
                if not block:
                    break
                digest.update(block)
                if label == 'rollback.json':
                    rollback_bytes.extend(block)
                    require(len(rollback_bytes) <= 4 * 1024 * 1024, 'rollback-json-too-large')
            hashes[label] = digest.hexdigest()

        try:
            inventory = json.loads(rollback_bytes, object_pairs_hook=no_duplicate_keys)
        except (ValueError, UnicodeError):
            raise CaptureBlocked('rollback-json-invalid') from None
        require(isinstance(inventory, dict), 'rollback-structure-invalid')
        files = inventory.get('files')
        require(type(inventory.get('schema_version')) is int and inventory['schema_version'] == 1 and
                inventory.get('revision') == LEGACY_REVISION and
                isinstance(files, dict) and set(files) == SEALED_FILES and
                all(isinstance(v, str) and re.fullmatch('[0-9a-f]{64}', v) for v in files.values()),
                'rollback-inventory-structure-invalid')

        # Refuse to approve a replaced path or a file changed during this capture.
        for label, parent, name, fd, before in opened:
            try:
                unchanged = (identity(os.fstat(fd)) == before and
                             identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) == before)
            except OSError:
                unchanged = False
            require(unchanged, label + '-changed-during-capture')
        for parent, name, fd, before in directory_links:
            try:
                unchanged = (identity(os.fstat(fd)) == before and
                             identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) == before)
            except OSError:
                unchanged = False
            require(unchanged, 'directory-changed-during-capture')
        return hashes


CHECKS = ('path/ownership checks', 'rollback inventory structure')
REMOTE_COMMAND = 'sudo -n env PYTHONDONTWRITEBYTECODE=1 python3 -B -'


def evidence(hashes):
    return {**{label + ' SHA256': hashes[label] for label, *_ in FILES},
            **{key: 'PASS' for key in CHECKS}}


def failure(reason, allowed):
    """Construct only a fixed reviewed failure document; never include exception text."""
    require(reason in allowed, 'unsafe-host-evidence')
    return {**{key: 'FAIL' for key in CHECKS}, 'reason': reason}


def safe_evidence(document, *, expected, failure_reasons=REMOTE_REASONS | RUNNER_REASONS):
    """No unvalidated SSH stdout, banners, partial hashes or exception text reach logs."""
    pass_keys = {label + ' SHA256' for label, *_ in FILES} | set(CHECKS)
    fail_keys = set(CHECKS) | {'reason'}
    if expected in ('pass', 'either') and isinstance(document, dict) and set(document) == pass_keys:
        require(all(document[key] == 'PASS' for key in CHECKS), 'unsafe-host-evidence')
        require(all(isinstance(document[label + ' SHA256'], str) and
                    re.fullmatch('[0-9a-f]{64}', document[label + ' SHA256'])
                    for label, *_ in FILES), 'unsafe-host-evidence')
        return document
    if expected in ('fail', 'either') and isinstance(document, dict) and set(document) == fail_keys:
        require(all(document[key] == 'FAIL' for key in CHECKS) and
                document['reason'] in failure_reasons, 'unsafe-host-evidence')
        return document
    raise CaptureBlocked('unsafe-host-evidence')


def inspect_once():
    """Runner only: existing strict transport, reviewed bytes sent once on stdin."""
    import subprocess
    import transport
    from evidence import api, master_gates
    require(os.environ.get('GITHUB_EVENT_NAME') == 'workflow_dispatch' and
            os.environ.get('GITHUB_REF') == 'refs/heads/master', 'control-gate-failed')
    sha = os.environ.get('GITHUB_SHA', '')
    require(re.fullmatch('[0-9a-f]{40}', sha), 'control-gate-failed')
    try:
        current = api('git/ref/heads/master')['object']['sha']
    except Exception:
        raise CaptureBlocked('control-gate-failed') from None
    require(current == sha, 'master-moved')
    try:
        master_gates(sha)
    except Exception:
        raise CaptureBlocked('control-gate-failed') from None
    source = Path(__file__).read_bytes()
    try:
        ssh = transport.configure()  # SSH material exists only in RUNNER_TEMP.
    except Exception:
        raise CaptureBlocked('ssh-config-unavailable') from None
    try:
        current = api('git/ref/heads/master')['object']['sha']
    except Exception:
        raise CaptureBlocked('control-gate-failed') from None
    require(current == sha, 'master-moved')
    try:
        result = subprocess.run(ssh + [REMOTE_COMMAND], input=source, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise CaptureBlocked('ssh-inspection-failed') from None
    if len(result.stdout) > 4096:
        raise CaptureBlocked('unsafe-host-evidence')
    try:
        document = json.loads(result.stdout, object_pairs_hook=no_duplicate_keys)
    except Exception:
        raise CaptureBlocked('unsafe-host-evidence') from None
    if result.returncode == 0:
        return safe_evidence(document, expected='pass')
    if result.returncode == 1:
        return safe_evidence(document, expected='fail', failure_reasons=REMOTE_REASONS)
    raise CaptureBlocked('ssh-inspection-failed')


def main():
    runner = sys.argv[1:] == ['--runner']
    try:
        if runner:
            document = inspect_once()
        else:
            require(len(sys.argv) == 1, 'no-path-overrides-permitted')
            document = evidence(capture())
        document = safe_evidence(document, expected='either')
    except CaptureBlocked as exc:
        allowed = RUNNER_REASONS if runner else REMOTE_REASONS
        reason = str(exc) if str(exc) in allowed else ('unsafe-host-evidence' if runner else 'capture-failed')
        document = failure(reason, allowed)
    except OSError:
        document = failure('unsafe-host-evidence' if runner else 'capture-failed',
                           RUNNER_REASONS if runner else REMOTE_REASONS)
    except Exception:
        document = failure('unsafe-host-evidence' if runner else 'capture-failed',
                           RUNNER_REASONS if runner else REMOTE_REASONS)
    print(json.dumps(document, sort_keys=True))
    return 0 if set(document) != set(CHECKS) | {'reason'} else 1


if __name__ == '__main__':
    raise SystemExit(main())
