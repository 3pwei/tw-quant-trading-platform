"""Explicit rollback refresh: preserve old bytes before atomic directory exchange.

No Legacy writes or lifecycle changes. Existing config/image identity must still
match. Only the sealed SQLite snapshot and its inventory digest can change.
"""
import ctypes
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile

import p9_validation as validation
import provision_host as provision
import readonly_sqlite

ROOT = provision.ROOT
LOCK = Path('/var/lock/tw-quant-deploy.lock')
FILES = (*provision.SEALED, 'rollback.json')
REASONS = frozenset('''refresh-invalid refresh-boundary refresh-prior-transaction
refresh-lock-unavailable refresh-approval-mismatch refresh-legacy-drift
refresh-snapshot-failed refresh-history-conflict refresh-archive-invalid
refresh-exchange-unavailable refresh-exchange-failed refresh-host-failed
refresh-incomplete-preparation'''.split())


class RefreshBlocked(Exception):
    pass


def require(condition, reason):
    if not condition:
        raise RefreshBlocked(reason)


def directory(path):
    require(path.is_dir() and not path.is_symlink() and path.resolve() == path and
            path.stat().st_uid == 0 and stat.S_IMODE(path.stat().st_mode) == 0o700,
            'refresh-boundary')


def no_transaction():
    # A refresh must never replace evidence belonging to a cutover/recovery.
    for name in ('transaction.json', 'acceptance.json', 'data', 'health',
                 'gateway-data', 'gateway-config', 'release.env'):
        require(not os.path.lexists(ROOT / name), 'refresh-prior-transaction')


def check_tree(root, pins, digest):
    directory(root)
    directory(root / 'rollback')
    require({p.name for p in (root / 'rollback').iterdir()} == set(FILES),
            'refresh-archive-invalid')
    for name in FILES:
        path = root / 'rollback' / name
        require(path.is_file() and not path.is_symlink() and path.resolve() == path and
                path.stat().st_nlink == 1, 'refresh-archive-invalid')
    previous = validation.ROOT
    try:
        validation.ROOT = root
        return validation.verify_backup(pins, digest)
    finally:
        validation.ROOT = previous


def copy_tree(source, target):
    target.mkdir(mode=0o700)
    for name in FILES:
        src = source / name
        # Source was validated while holding the deployment lock. Copy, no links.
        with src.open('rb') as inp, (target / name).open('xb') as out:
            shutil.copyfileobj(inp, out, length=1024 * 1024)
            os.fchmod(out.fileno(), 0o600)
            os.fsync(out.fileno())
    provision.fsync_dir(target)


def exchange(source, target):
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, 'renameat2', None)
    require(rename is not None, 'refresh-exchange-unavailable')
    # Both paths are on the same filesystem under ROOT; never unlink canonical.
    result = rename(provision.AT_FDCWD, os.fsencode(source), provision.AT_FDCWD,
                    os.fsencode(target), 2)  # RENAME_EXCHANGE
    require(result == 0, 'refresh-exchange-failed')


def legacy(pins, old):
    state = provision.current_state(pins)
    revision, containers, market, worker, health = state
    require(revision == old['revision'] and
            all(containers[s]['Id'] == old['containers'][s]['id'] and
                containers[s]['Image'] == old['containers'][s]['image_id']
                for s in provision.SERVICES) and
            provision.sha(provision.LEGACY / 'deployments/current.env') == old['record_sha256'],
            'refresh-legacy-drift')
    return state


def refresh(payload):
    require(isinstance(payload, dict) and set(payload) ==
            {'control_sha', 'pins', 'hashes', 'previous_sha256'}, 'refresh-invalid')
    previous = payload['previous_sha256']
    require(isinstance(previous, str) and re.fullmatch('[0-9a-f]{64}', previous) and
            isinstance(payload['control_sha'], str) and
            re.fullmatch('[0-9a-f]{40}', payload['control_sha']), 'refresh-invalid')
    require(os.geteuid() == 0, 'refresh-boundary')
    for path in (ROOT, ROOT / 'config', ROOT / 'provider', ROOT / 'rollback'):
        directory(path)
    no_transaction()
    require(LOCK.is_file() and not LOCK.is_symlink() and LOCK.resolve() == LOCK,
            'refresh-lock-unavailable')
    with LOCK.open('rb') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RefreshBlocked('refresh-lock-unavailable') from None
        return refresh_locked(payload)


def refresh_locked(payload):
    pins, hashes, previous = payload['pins'], payload['hashes'], payload['previous_sha256']
    no_transaction()
    # Never silently reuse or delete a previous interrupted preparation.
    require(not list(ROOT.glob('.rollback-refresh-*')), 'refresh-incomplete-preparation')
    try:
        envs = validation.config_check(pins, hashes)
        old = check_tree(ROOT, pins, previous)
        state = legacy(pins, old)
        require(envs['market.env']['MARKET_DATA_PROVIDER'] == 'shioaji', 'refresh-legacy-drift')
    except RefreshBlocked:
        raise
    except Exception:
        raise RefreshBlocked('refresh-approval-mismatch') from None
    try:
        image, inventory = provision.stable_database(state[1], state[2], state[3])
    except Exception:
        raise RefreshBlocked('refresh-snapshot-failed') from None
    history = ROOT / 'rollback-history'
    if not os.path.lexists(history):
        history.mkdir(mode=0o700)
        provision.fsync_dir(ROOT)
    directory(history)
    archived = history / previous
    temporary = Path(tempfile.mkdtemp(prefix='.rollback-refresh-', dir=ROOT))
    exchanged = False
    try:
        os.chmod(temporary, 0o700)
        # Archive the old complete inventory first; never overwrite a version.
        if os.path.lexists(archived):
            try:
                check_tree(archived, pins, previous)
            except Exception:
                raise RefreshBlocked('refresh-history-conflict') from None
        else:
            archive_stage = temporary / 'archive'
            archive_stage.mkdir(mode=0o700)
            copy_tree(ROOT / 'rollback', archive_stage / 'rollback')
            check_tree(archive_stage, pins, previous)
            provision.fsync_dir(archive_stage)
            provision.publish_noreplace(archive_stage, archived)
            provision.fsync_dir(history)
        candidate = temporary / 'candidate'
        candidate.mkdir(mode=0o700)
        copy_tree(ROOT / 'rollback', candidate / 'rollback')
        # Only these two unexposed preparation files are replaced.
        (candidate / 'rollback/data.sqlite3').unlink()
        provision.write_file(candidate / 'rollback/data.sqlite3', image)
        require(readonly_sqlite.snapshot(candidate / 'rollback/data.sqlite3') == inventory,
                'refresh-snapshot-failed')
        updated = dict(old, files=dict(old['files']))
        updated['files']['data.sqlite3'] = provision.sha(candidate / 'rollback/data.sqlite3')
        (candidate / 'rollback/rollback.json').unlink()
        provision.write_file(candidate / 'rollback/rollback.json',
                             (json.dumps(updated, sort_keys=True) + '\n').encode())
        new_digest = provision.sha(candidate / 'rollback/rollback.json')
        check_tree(candidate, pins, new_digest)
        # Detect changed approvals, Legacy identity, or DB during capture.
        validation.config_check(pins, hashes)
        check_tree(ROOT, pins, previous)
        check_tree(archived, pins, previous)
        after = legacy(pins, old)
        require(all(after[1][s]['State']['StartedAt'] == state[1][s]['State']['StartedAt']
                    for s in provision.SERVICES), 'refresh-legacy-drift')
        final_image, final_inventory = provision.stable_database(after[1], after[2], after[3])
        require(final_image == image and final_inventory == inventory, 'refresh-snapshot-failed')
        no_transaction()
        for path in (candidate / 'rollback', candidate, temporary):
            provision.fsync_dir(path)
        if new_digest != previous:
            exchange(candidate / 'rollback', ROOT / 'rollback')
            exchanged = True
            provision.fsync_dir(ROOT)
            provision.fsync_dir(candidate)
        check_tree(ROOT, pins, new_digest)
        # Any post-exchange failure leaves the archive and preparation intact.
        result = {'status': 'PASS', 'previous_sha256': previous,
                  'rollback_sha256': new_digest, 'changed': new_digest != previous,
                  'config_sha256': hashes, 'execution_locked': True,
                  'external_order_calls': 0, 'external_cancel_calls': 0,
                  'approval_updated': False}
    except Exception:
        if not exchanged:
            shutil.rmtree(temporary)
        raise
    else:
        shutil.rmtree(temporary)
        provision.fsync_dir(ROOT)
        return result
