# P9 PoC clean Production deployment

This is the approved path for the current showcase-only Production. It creates a
new `/data/platform.sqlite3`; it does **not** merge, copy, refresh, or compare
Legacy application data. The existing migration-oriented `P9 Production Cutover`
workflow remains documented for historical review and must not be substituted for
this flow.

No workflow in this document is authorized merely because the source exists.
Merge, Candidate, P8, Production configuration, and deployment each require their
own later approval.

## Fixed scope

- Reuse the isolated Production Lightsail host.
- Stop the retained Legacy containers before copying state or starting P9. Legacy
  and P9 never serve the ingress concurrently.
- Preserve two independently resolved, integrity-checked SQLite backups: market
  and execution. Capture both even if they resolve to the same underlying file.
- Preserve the verified config archive, exact image archives, Legacy revision,
  deployment-record digest, container IDs, image IDs, and config digests in the
  root-only `legacy-clean-backup/` directory.
- Initialize exactly one fresh administrator from
  `PLATFORM_BOOTSTRAP_ADMIN_EMAILS`. Never guess, print, or publish the value.
- Keep Cloudflare Access JWT validation and Email One-time PIN policy external.
- Keep real-order execution disabled/locked. The worker remains networkless,
  `BROKER_PROVIDER=disabled`, and every live feature flag remains false.
- Use Shioaji only for market data. `MARKET_SJ_*` may exist only in `market.env`;
  execution and gateway config reject both `MARKET_SJ_*` and `SJ_*` keys.
- Keep provider factory, strategies, composites, and real parameters external.

## Why `replay.csv` is not required

This path is explicitly Shioaji market-data mode and never switches to replay or
mock to pass a gate. It does not mount `replay.csv`, omits it from the four approved
config hashes, and rejects a nonempty `MARKET_REPLAY_CSV`. A future replay design
requires separate acceptance; do not manufacture placeholder CSV data.

## Required new P8 binding

Runtime and Production control-plane bytes change. Before dispatch:

1. a new Candidate built from the merged clean-deploy/Demo-boundary revision;
2. P8 A deploy/restart, B deploy/restart, rollback/restart, 60-minute soak,
   Shioaji 1.7.4 offline capability, image/SBOM/provenance/leakage, gateway,
   execution-lock, credential-isolation, and Demo redaction acceptance;
3. a reviewed binding commit that changes only
   `deploy/production/approved-p8.json` and `public-candidate.json` after the P8
   source; any other source change forces another Candidate/P8;
4. successful CI and Security for both the exact P8 source and binding master.

Until the new binding is committed, `P9 PoC Clean Deploy` stops before Production
host access. It accepts the P8 source only across the two-file binding-only diff.

## First-time Production configuration preparation

The clean path does not run or consume `Production Prerequisite Provision`. On the
first run, `/srv/trading-platform-production` must not exist. Dispatch the separate
manual `P9 PoC Clean Prepare` workflow only after the new P8 binding and its CI and
Security gates are complete. Its confirmation is:

```text
PREPARE P9 POC CLEAN production <current-master-sha>
```

The preparation transaction takes exactly four Production-only base64 secrets,
validates them against the bound runtime, and atomically creates only root-owned
mode-700 `config/` and `provider/` under the new root:

- `PRODUCTION_POC_MARKET_ENV_B64`;
- `PRODUCTION_POC_EXECUTION_ENV_B64`;
- `PRODUCTION_POC_GATEWAY_ENV_B64`;
- `PRODUCTION_POC_PROVIDER_FACTORY_B64`.

It refuses an existing root or abandoned `.trading-platform-production.clean-prepare-*`
state. It does not inspect, stop, or alter Legacy; create a DB; accept `replay.csv`;
or create a `rollback/` directory. Its only artifact is a bounded status document
containing the four SHA-256 values, never configuration bytes or the admin email.
After independent review, set those exact values as
`PRODUCTION_POC_APPROVED_CONFIG_SHA256_JSON`. Do not guess missing values and do not
reuse Staging sources.

Both workflows use these `lightsail-production` Environment values:

- `PRODUCTION_HOST`, `PRODUCTION_USER`, distinct `STAGING_HOST_IDENTITY`;
- `PRODUCTION_SSH_PRIVATE_KEY`, `PRODUCTION_SSH_HOST_KEY`.

The deployment workflow additionally requires:

- `PUBLIC_DASHBOARD_URL`;
- `PRODUCTION_POC_APPROVED_CONFIG_SHA256_JSON` with exactly `market.env`,
  `execution.env`, `gateway.env`, and `factory` SHA-256 values.

Do not add `replay.csv`. Do not reuse Staging config/provider files.

Required `market.env` values include:

```text
PLATFORM_ENVIRONMENT=production
PLATFORM_AUTHORIZATION_MODE=enforced
MARKET_ACCESS_MODE=cloudflare
CF_ACCESS_TEAM_DOMAIN=<existing Access team domain>
CF_ACCESS_AUD=<existing Access application audience>
PLATFORM_BOOTSTRAP_ADMIN_EMAILS=<existing admin email; exactly one>
MARKET_DATA_PROVIDER=shioaji
MARKET_SJ_API_KEY=<market-data credential>
MARKET_SJ_SECRET_KEY=<market-data credential>
MARKET_SJ_PRODUCTION=true
MARKET_DB_PATH=/data/platform.sqlite3
PRIVATE_PROVIDER_WHEEL_SHA256=<new accepted P8 digest>
```

`MARKET_REPLAY_CSV` must be absent or empty. `execution.env` requires
`LIVE_EXECUTION_DB_PATH=/data/platform.sqlite3`,
`LIVE_EXECUTION_HEALTH_PATH=/run/tw-quant-execution/health.json`,
`BROKER_PROVIDER=disabled`, and all live flags false. Execution and gateway must
not contain Shioaji credential keys.

## Transaction order

1. Verify exact master/P8 identity, CI/Security, artifacts, four config digests,
   host separation, lock, current Legacy revision and deployment-record digest,
   exact container/image identities, Cloudflare settings, provider digest,
   Shioaji capability, and execution lock. No old rollback inventory is read.
2. Refuse prior clean transaction/acceptance/failure/backup/data/health/gateway
   state or partial prepare directories. Nothing is overwritten automatically.
3. Write `transaction-clean.json`, then stop the exact Legacy containers.
4. Atomically create `legacy-clean-backup/`: SQLite-backup and integrity-check the
   independently resolved market and execution DBs, directly archive the four
   current Legacy config files, and run `docker image save` for each exact retained
   image. Nothing is copied from an old `rollback/`. Verify the new inventory and
   write its digest to root-only `backup-clean.json`; backup bytes and inventory are
   never uploaded.
5. Verify exact images and offline Shioaji capability. Create new empty runtime
   directories. Copy only Caddy state for TLS continuity; copy no Legacy DB.
6. Run a no-network one-shot initialization in the exact runtime image. Refuse an
   existing `platform.sqlite3`; create application schemas and verify exactly one
   active admin with trading disabled.
7. Validate and start services; verify images, auth/owner boundaries, locked worker
   with zero external calls, fresh DB, restart continuity, and public origin.
8. Publish bounded status evidence only—never config values, email, credentials,
   strategies, DB contents, or backup files.

## Repeat and failure behavior

A repeat of preparation fails `production-root-exists`; a repeat deployment fails
`clean-environment-already-initialized`. Neither clears or replaces an existing DB.
A mid-transaction failure records a sanitized invariant, stops P9, restarts the exact
Legacy IDs recorded before the stop, and retains partial P9 state and backup for
review. If backup capture finished, recovery re-verifies its new inventory digest;
if capture failed before publication, recovery still uses the pre-stop identity
journal and reports that no completed backup was verified.

Do not rerun a partial transaction. Confirm Legacy health; inspect root-only
`transaction-clean.json`, `failure-clean.json`, and
`rollback-clean-result.json` (plus `backup-clean.json` when present). Archive partial
data/health/gateway/bundle/backup to
an operator-chosen root-only incident path. Only separate authorization may remove
those paths and markers. There is intentionally no automatic cleanup command.

Explicit recovery is:

```text
python deploy/production/poc_clean_transport.py recover --evidence p9-clean-evidence
```

It starts exact retained Legacy container IDs; it does not rebuild, restore mutable
tags, change configuration, or erase the new DB.

## Deployment-only acceptance

Only an authorized deployment can verify real Cloudflare Email OTP login for the
configured admin and live Shioaji market flow. Unit tests cover configuration,
admin role/status/trading lock, credential separation, recovery, repeat refusal,
and Demo output boundaries; they do not claim OTP or live-market acceptance.
