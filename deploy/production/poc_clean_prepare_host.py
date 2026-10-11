"""Atomic first-time configuration preparation for the P9 clean PoC path."""
from __future__ import annotations

import base64
import ctypes
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile

import poc_clean


ROOT = Path('/srv/trading-platform-production')
PARENT = ROOT.parent
LOCK = Path('/var/lock/tw-quant-deploy.lock')
SOURCE_NAMES = frozenset({'market.env', 'execution.env', 'gateway.env', 'factory'})
VALIDATION_REASONS = frozenset({
    'clean-config-approval-missing', 'config-isolation', 'config-digest',
    'factory-invalid', 'clean-market-provider-not-shioaji',
    'clean-required-setting-missing', 'clean-shioaji-production-required',
    'clean-production-auth-missing', 'clean-admin-email-invalid',
    'clean-shioaji-replay-not-required', 'provider-config-provenance',
    'production-data-path', 'market-credential-isolation', 'staging-config-reuse',
    'real-order-enabled', 'live-confirmation-present', 'execution-not-disabled',
})
AT_FDCWD = -100
RENAME_NOREPLACE = 1


class PrepareBlocked(Exception):
    pass


def require(condition, reason):
    if not condition:
        raise PrepareBlocked(reason)


def no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'payload-invalid')
        result[key] = value
    return result


def decode_sources(sources):
    require(isinstance(sources, dict) and set(sources) == SOURCE_NAMES,
            'payload-invalid')
    result = {}
    for name, encoded in sources.items():
        require(isinstance(encoded, str) and len(encoded) <= 2 * 1024 * 1024 and
                re.fullmatch('[A-Za-z0-9+/=]+', encoded) is not None,
                'payload-invalid')
        try:
            data = base64.b64decode(encoded, validate=True)
        except Exception:
            raise PrepareBlocked('payload-invalid') from None
        require(data and len(data) <= 1024 * 1024, 'payload-invalid')
        result[name] = data
    return result


def write_file(path, data, *, uid=0):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                         os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        os.fchown(descriptor, uid, uid if uid else 0)
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            require(written > 0, 'config-write-failed')
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def fsync_dir(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def publish_noreplace(source, target):
    function = getattr(ctypes.CDLL(None, use_errno=True), 'renameat2', None)
    require(function is not None, 'atomic-publish-unavailable')
    require(function(AT_FDCWD, os.fsencode(source), AT_FDCWD, os.fsencode(target),
                     RENAME_NOREPLACE) == 0, 'atomic-publish-failed')


def prepare(payload):
    require(isinstance(payload, dict) and set(payload) == {'control_sha', 'pins', 'sources'} and
            re.fullmatch('[0-9a-f]{40}', str(payload.get('control_sha'))) is not None and
            isinstance(payload.get('pins'), dict), 'payload-invalid')
    sources = decode_sources(payload['sources'])
    require(LOCK.is_file() and not LOCK.is_symlink(),
            'deployment-lock-unavailable')
    temporary = None
    with LOCK.open('rb') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise PrepareBlocked('deployment-lock-unavailable') from None
        require(PARENT.is_dir() and not PARENT.is_symlink() and
                PARENT.resolve() == PARENT and PARENT.stat().st_uid == 0,
                'production-parent-invalid')
        require(not ROOT.exists() and not ROOT.is_symlink(), 'production-root-exists')
        require(not list(PARENT.glob('.trading-platform-production.clean-prepare-*')),
                'production-prepare-state-exists')
        temporary = Path(tempfile.mkdtemp(
            prefix='.trading-platform-production.clean-prepare-', dir=PARENT))
        try:
            os.chmod(temporary, 0o700)
            for name in ('config', 'provider'):
                (temporary / name).mkdir(mode=0o700)
            for name in ('market.env', 'execution.env', 'gateway.env'):
                write_file(temporary / 'config' / name, sources[name])
            write_file(temporary / 'provider/factory', sources['factory'], uid=10001)
            hashes = {
                name: sha256(temporary / ('provider/factory' if name == 'factory'
                                           else 'config/' + name))
                for name in sorted(SOURCE_NAMES)
            }
            original_root = poc_clean.ROOT
            try:
                poc_clean.ROOT = temporary
                try:
                    poc_clean.clean_config_check(payload['pins'], hashes)
                except ValueError as exc:
                    reason = str(exc)
                    if reason in VALIDATION_REASONS:
                        raise PrepareBlocked(reason) from None
                    raise PrepareBlocked('config-validation-failed') from None
                except Exception:
                    raise PrepareBlocked('config-validation-failed') from None
            finally:
                poc_clean.ROOT = original_root
            for directory in (temporary, temporary / 'config', temporary / 'provider'):
                require(directory.stat().st_uid == 0 and
                        stat.S_IMODE(directory.stat().st_mode) == 0o700,
                        'owner-mode-invalid')
                fsync_dir(directory)
            require(not ROOT.exists() and not ROOT.is_symlink(), 'production-root-exists')
            publish_noreplace(temporary, ROOT)
            temporary = None
            fsync_dir(PARENT)
            return {
                'status': 'PASS',
                'root': str(ROOT),
                'config_sha256': hashes,
                'market_provider': 'shioaji',
                'real_order': 'disabled',
            }
        finally:
            if temporary is not None and temporary.exists():
                shutil.rmtree(temporary)
