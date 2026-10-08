# Versioned Production rollback snapshot refresh

`Production Rollback Snapshot Refresh` is a separately authorized **writer** for
an already provisioned Production root. It is not an Inventory/Preflight action,
and merging this workflow does not authorize dispatch. Real orders stay disabled.

## Request and approval boundary

Use current `master` with successful CI and Security. The `lightsail-production`
environment must already contain `PRODUCTION_APPROVED_CONFIG_SHA256_JSON` and
`LEGACY_ROLLBACK_INVENTORY_SHA256` from reviewed Inventory evidence. The request
must contain the exact current approved rollback digest as `previous_sha256` and:

```text
REFRESH production rollback <exact-master-sha> from <approved-rollback-sha256>
```

The runner checks master before preparing SSH and immediately before executing.
The job uses the same Production concurrency group and existing exclusive host
deployment lock. No automatic trigger, retry, approval-variable update, subsequent
workflow dispatch, image pull/build, service stop/start, broker login/order/cancel,
or Legacy config/database write is introduced.

## Files and publication

The existing canonical path remains `rollback/`, so Inventory, Preflight, Cutover
and rollback recovery retain their reviewed path and digest contracts.

1. Require safe root/config/provider/rollback ownership and permissions, approved
   config hashes, the exact old rollback digest, current Legacy revision,
   container/image identities, locked execution, zero order/cancel calls and
   matching Shioaji provider. Refuse transaction/acceptance/runtime leftovers.
2. Capture a stable SQLite/WAL committed image in memory using the existing
   strict reader; require integrity PASS and locked targets.
3. Preserve a complete copy at
   `rollback-history/<old-inventory-sha256>/rollback/`. This contains all original
   SQLite/config/image archives and the original `rollback.json` bytes. Existing
   versions are validated and never overwritten. No symlinks or hardlinks.
4. Prepare another complete rollback directory under a unique root-only
   `.rollback-refresh-*` directory. Replace only its unexposed SQLite file and
   inventory digest. Validate every file, configuration and identity again, and
   reject DB or container-generation changes during preparation.
5. Flush files/directories, then atomically exchange the prepared rollback with
   canonical `rollback/` using Linux `renameat2(RENAME_EXCHANGE)`. Canonical
   rollback is never absent. If the digest is unchanged, skip exchange.
6. Report only bounded, sanitized evidence. The old approved digest remains
   unchanged; changed canonical bytes cannot pass it. Retain the old archive.

A subsequent successful refresh archives the then-current canonical version;
all older versions remain intact. Provisioning itself still refuses an existing
root. This workflow does not delete the Production root or relax that guard.

## After success

Review the reported previous/new digests, then authorize a **fresh** read-only
Approval Inventory. Only after independent evidence review should the approved
rollback variable change. Authorize a fresh Preflight separately. Do not rerun
Inventory #4 or Preflight #3 as part of this operation.

Refresh does not freeze application writes. A snapshot may become stale again;
Preflight's full-table comparison and Cutover's post-quiescence drift check remain
mandatory. A maintenance/write-quiescence policy is still needed for guaranteed
progress under continuous writes. This change supplies a refresh entry point,
not permission to bypass stale-backup checks or assurance that P9 is ready.

## Failure and recovery

Before exchange, failures preserve canonical rollback. An archive may already
have been created and is retained. After exchange, a failure or lost SSH response
may mean the new canonical snapshot is already installed. Never infer zero writes
from BLOCKED. Verify canonical and archived hashes through separately authorized
read-only inspection before deciding any further action.

An interrupted `.rollback-refresh-*` preparation blocks another refresh. Preserve
it for inspection; do not manually remove it to pass the gate or automatically
retry. This workflow does not implement cleanup of incomplete refreshes or
post-Cutover retry. Old approval variables stay unchanged in every failure path.

The archive and preparation use full copies (no shared inodes); sufficient disk
space is required. Disk/copy/fsync/exchange failures fail closed. The existing
SQLite/WAL size limits remain unchanged. History retention/pruning requires a
separate policy and explicit action.
