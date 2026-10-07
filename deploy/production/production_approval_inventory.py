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

ROOT_PARTS = ('srv', 'trading-platform-p9')
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
        def opened_fd(path, flags, parent=None):
            fd = os.open(path, flags, dir_fd=parent)
            stack.callback(os.close, fd)
            return fd

        current = opened_fd('/', dir_flags)
        for component in _root_parts:
            parent = current
            current = opened_fd(component, dir_flags, parent)
            s = os.fstat(current)
            require(stat.S_ISDIR(s.st_mode), 'root-not-directory')
            directory_links.append((parent, component, current, identity(s)))
        root = current
        s = os.fstat(root)
        require(s.st_uid == 0 and stat.S_IMODE(s.st_mode) == 0o700, 'production-root-owner-mode')

        parents = {}
        for directory in ('config', 'provider', 'rollback'):
            fd = opened_fd(directory, dir_flags, root)
            s = os.fstat(fd)
            require(s.st_uid == 0 and stat.S_IMODE(s.st_mode) == 0o700, directory + '-owner-mode')
            parents[directory] = fd
            directory_links.append((root, directory, fd, identity(s)))

        # Validate all six paths before reading a single byte for hashing.
        for label, directory, name, uid in FILES:
            parent = parents[directory]
            before = os.stat(name, dir_fd=parent, follow_symlinks=False)
            require(stat.S_ISREG(before.st_mode), label + '-not-regular-or-symlink')
            require(before.st_nlink == 1, label + '-shared-file')
            require(before.st_uid == uid and stat.S_IMODE(before.st_mode) in (0o400, 0o600),
                    label + '-owner-mode')
            fd = opened_fd(name, file_flags, parent)
            require(identity(os.fstat(fd)) == identity(before), label + '-changed-before-read')
            opened.append((label, parent, name, fd, identity(before)))

        hashes = {}
        rollback_bytes = bytearray()
        for label, parent, name, fd, before in opened:
            digest = hashlib.sha256()
            while True:
                block = os.read(fd, 1024 * 1024)
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
            require(identity(os.fstat(fd)) == before and
                    identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) == before,
                    label + '-changed-during-capture')
        for parent, name, fd, before in directory_links:
            require(identity(os.fstat(fd)) == before and
                    identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) == before,
                    'directory-changed-during-capture')
        return hashes


CHECKS = ('path/ownership checks', 'rollback inventory structure')
REMOTE_COMMAND = 'sudo -n env PYTHONDONTWRITEBYTECODE=1 python3 -B -'


def evidence(hashes):
    return {**{label + ' SHA256': hashes[label] for label, *_ in FILES},
            **{key: 'PASS' for key in CHECKS}}


def safe_evidence(document):
    """No unvalidated SSH stdout, banners or exception messages reach logs."""
    require(isinstance(document, dict) and
            set(document) == {label + ' SHA256' for label, *_ in FILES} | set(CHECKS),
            'unsafe-host-evidence')
    require(all(document[key] == 'PASS' for key in CHECKS), 'unsafe-host-evidence')
    require(all(isinstance(document[label + ' SHA256'], str) and
                re.fullmatch('[0-9a-f]{64}', document[label + ' SHA256'])
                for label, *_ in FILES), 'unsafe-host-evidence')
    return document


def inspect_once():
    """Runner only: existing strict transport, reviewed bytes sent once on stdin."""
    import subprocess
    import transport
    from evidence import api, master_gates
    require(os.environ.get('GITHUB_EVENT_NAME') == 'workflow_dispatch' and
            os.environ.get('GITHUB_REF') == 'refs/heads/master', 'manual-master-required')
    sha = os.environ.get('GITHUB_SHA', '')
    require(re.fullmatch('[0-9a-f]{40}', sha), 'control-sha-invalid')
    require(api('git/ref/heads/master')['object']['sha'] == sha, 'master-moved')
    master_gates(sha)
    source = Path(__file__).read_bytes()
    ssh = transport.configure()  # SSH material exists only in RUNNER_TEMP.
    require(api('git/ref/heads/master')['object']['sha'] == sha, 'master-moved')
    result = subprocess.run(ssh + [REMOTE_COMMAND], input=source, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, timeout=120, check=False)
    require(result.returncode == 0 and len(result.stdout) <= 4096, 'ssh-inspection-failed')
    document = json.loads(result.stdout, object_pairs_hook=no_duplicate_keys)
    return safe_evidence(document)


def main():
    try:
        if sys.argv[1:] == ['--runner']:
            document = inspect_once()
        else:
            require(len(sys.argv) == 1, 'no-path-overrides-permitted')
            document = evidence(capture())
        print(json.dumps(safe_evidence(document), sort_keys=True))
        return 0
    except Exception:
        # Fixed checks only, with no partial hashes or raw host/exception output.
        print(json.dumps({key: 'FAIL' for key in CHECKS}, sort_keys=True))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
