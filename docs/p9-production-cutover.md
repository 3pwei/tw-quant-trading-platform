# P9 Production Cutover — review gate

This PR prepares a manual workflow. Merging is not authorization to dispatch.
No Production run, live order, live canary, Legacy retirement or P10 is part of
this change. P8 remains complete.

## Fixed artifact and control-plane identities

`deploy/production/approved-p8.json` binds the exact P8 source
`f70c1ba2fbfd6f5deff3178361d18f576af94b24`, Candidate #20 run `37306446381`,
Staging #15 run `37307300334`, manifest checksum and GitHub artifact ZIP digests.
The acceptance archive contains four individually pinned files. The ledger and
manifest hashes must agree with the acceptance report. Expired/missing artifacts
fail; replacing them, rerunning Candidate or rebuilding is not a fallback.

Production deploys runtime A (`known_good`), which is the final P8 rollback/soak
release, and the exact accepted gateway image. Runtime B remains recorded in the
pinned manifest but is not a Production rollback target. The rollback target is
Legacy Production revision `683bb4ebc4c4980480a4786136701ff458338a14`.

The workflow/control source SHA after merge differs from the runtime SHA. Both
must have successful latest exact-master push CI and Security runs. The dispatch
control SHA must still be master immediately before host writes. The source must
be its ancestor. No runtime, Dashboard, Core or Private Strategies bytes change.

Confirmation must be exactly:

```text
DEPLOY production f70c1ba2fbfd6f5deff3178361d18f576af94b24 candidate 37306446381 manifest 157298f60fd8e3067fadb73e1e5b0fab08537f1dafe8ae74d6f38276610fe3ad
```

## Separate Production prerequisites

These prerequisites must be prepared and separately reviewed before a future
dispatch. This PR does not provision them or assert they already exist.

Use GitHub environment `lightsail-production`, with Production-only credentials:

- Secrets `P9_PRODUCTION_SSH_PRIVATE_KEY`, `P9_PRODUCTION_SSH_HOST_KEY`.
- Variables `P9_PRODUCTION_HOST`, `P9_PRODUCTION_USER`,
  `P9_STAGING_HOST_IDENTITY` (the actual Staging host, which must differ),
  `PUBLIC_DASHBOARD_URL` (the existing HTTPS Production origin).
- `P9_PRODUCTION_CONFIG_SHA256_JSON`: approved SHA256 values keyed by
  `market.env`, `execution.env`, `gateway.env`, `factory`, `replay.csv`.
- `P9_PRODUCTION_ROLLBACK_SHA256`: approved SHA256 of the sealed rollback inventory.

Host files are independently provisioned under `/srv/trading-platform-p9`.
The root and its `config`, `provider`, `rollback` directories must be root-owned
mode 700. The separate host transaction has a 15-minute deadline; TERM invokes
compensation, with an additional 90 seconds before forced termination.
Config files are regular, root-owned mode 400/600 files in `config/`; factory is a
regular UID 10001-owned mode 400/600 file at `provider/factory`. Symlinks and
Staging paths/config markers are rejected. Market must enforce Production
Cloudflare authentication/authorization and declare the pinned provider digest.
The independently provisioned `config/replay.csv` must be UID 10001-owned mode
400/600 and its checksum must match the existing Production replay file. Market
must use `MARKET_DATA_PROVIDER=replay` and
`MARKET_REPLAY_CSV=/run/production-market/replay.csv`.

**Capability review gate:** the approved P8 Dockerfile installs `server` extras,
not the broker SDK extra present in the Legacy image. These exact immutable P8
images therefore cannot preserve an SDK-backed Production market feed. Preflight
hard-fails if the current Production provider is not replay/mock; do not switch
the feed to replay to pass this gate. If Production currently uses a broker SDK,
a separately authorized image/Candidate/P8 acceptance cycle is required before
P9 can be dispatched. This PR never installs packages or rebuilds to bypass it.

Both new services use `/data/platform.sqlite3`; worker uses the generation-health
path `/run/tw-quant-execution/health.json`. Worker must set
`BROKER_PROVIDER=disabled` and `LIVE_TRADING_ENABLED=false`. All live/shadow,
canary, auto, guardian and broker-read-only flags must be false; live confirmation
must be empty. No broker credential mount is added. The factory is the separately
provisioned Production reference to the already-built provider, not a Staging
secret transfer.

`rollback/rollback.json` is root-owned mode 400/600, with this schema:

```json
{
  "schema_version": 1,
  "revision": "683bb4ebc4c4980480a4786136701ff458338a14",
  "domain": "tmf.milespapa.com",
  "record_sha256": "<SHA256 of existing Legacy deployments/current.env>",
  "containers": {
    "market-api": {"id": "<exact 64-hex container ID>", "image_id": "sha256:<local image ID>", "config_digest": "sha256:<original config bytes hash>"},
    "execution-worker": {"id": "<exact 64-hex container ID>", "image_id": "sha256:<local image ID>", "config_digest": "sha256:<original config bytes hash>"},
    "gateway": {"id": "<exact 64-hex container ID>", "image_id": "sha256:<local image ID>", "config_digest": "sha256:<original config bytes hash>"}
  },
  "files": {
    "data.sqlite3": "<SHA256>",
    "config.tar": "<SHA256>",
    "image-market-api.tar": "<SHA256>",
    "image-execution-worker.tar": "<SHA256>",
    "image-gateway.tar": "<SHA256>"
  }
}
```

All listed backup files reside in `rollback/`, root-owned mode 400/600. Image
archives are independent single-image `docker image save` archives, whose actual
config bytes are hashed with the existing offline P8 helper. `config.tar` is an
uncompressed archive of exactly the four regular Legacy
`config/{compose,market,execution,gateway}.env` files under `/opt/tw-quant`,
matching current bytes. The Legacy approved revision uses root `/opt/tw-quant`
and Compose project `tw-quant-lightsail`; it is not the newer Public repository
Production root/project. The exact worker database path is read from its retained
container config, never guessed from the new runtime filename. Current market and
worker must share that same database; separate databases require a separately
reviewed migration and are rejected rather than silently dropping market/user state.
`data.sqlite3` is a consistent SQLite backup with integrity PASS and all execution
targets locked. Its complete logical table/schema inventory must match current
Production during read-only preflight **and again after stopping Legacy**. Any
intervening application write fails before deploying the candidate and resumes
Legacy. A fresh, quiescent backup window is therefore required; no stale-backup
or data-loss exception is permitted.

## Transaction and recovery

1. Verify exact tree, manual identity, master gates, P8 workflow/artifact identities,
   ZIP checksums, each acceptance file checksum and provenance before host access.
2. Run reviewed preflight from stdin without writing Production files. Verify
   current Legacy git/record/container/image identities, health/locked zero-call
   worker, independent config/secrets, image backups and SQLite state.
3. Recheck master gates before control bundle upload. Under the same host lock as
   the Legacy deploy workflow, repeat host preflight, write an fsynced recovery
   journal, and stop the original containers without deleting them or volumes.
4. Recheck database continuity. Copy only the verified Production backup into a
   newly created P9 data directory; create independent health/gateway directories and copy TLS state from the stopped
   Legacy gateway volumes without changing those original volumes.
5. Pull pinned image references; verify canonical registry digests, offline config
   digests, source and Core/Private Strategies/P7 labels. There is no build step,
   build context or mutable-tag fallback. Registry auth is temporary and removed.
6. Validate Production config and Caddy overlay, deploy with `--no-build --pull
   never`, require actual health, locked/disabled worker and zero order/cancel calls.
7. Restart and require same container/image identities, new process generation,
   fresh heartbeat and unchanged durable targets/orders/outboxes/fills/positions.
   Live reconciliation is explicitly not applicable while disabled; durable
   reconciliation and generation checks remain required.
8. Verify HTTPS/auth boundaries locally and the formal public origin from the
   external runner while retaining the same host flock. The authenticated SSH
   session supplies an exact-manifest acknowledgement; a negative/missing response
   fails and compensates. Only then reverify unchanged runtime, Legacy rollback
   identity/config and write acceptance. There is no unlocked finalize window.

The accepted P8 gateway has a Staging Caddyfile embedded in its image. Production
mounts a reviewed Production Caddyfile preserving the existing TLS, forward-auth,
public Demo exceptions, security headers and request limits. This is an explicit
Production configuration overlay, not an image rebuild or direct Staging ingress
reuse. The healthcheck retains its five-second timeout and requires healthy.

On the first forward failure, do not advance or rerun. Evidence capture, validation
and publication failures also invoke recovery; publication is not retried. Stop only P9 containers and
start the exact retained Legacy container IDs. Verify their original image IDs,
revision, record/config bytes, HTTPS health, worker lock/zero calls and durable
state. No git reset, Legacy rebuild, tag re-resolution or restore into Legacy data
occurs. Original mounts/data remain untouched. The external runner also verifies
the recovered public origin. Failed acceptance is removed; failure/rollback
reports remain. Recovery is independently invoked after a failed/interrupted SSH
transaction using the fsynced journal and sealed inventory. Recovery failure is a
hard failure; manual incident recovery is required if SSH/host/storage itself is
unavailable. Do not remove the retained Legacy containers, images, volumes or
backup archives after success; that belongs to a separately authorized P10.

Acceptance is stored separately in `/srv/trading-platform-p9/acceptance.json`;
Legacy git and deployment record remain intact as recovery provenance. The P9
record identifies the runtime source, control/master gates, manifest, backup,
pre/post restart observations and external runner gate. Uploaded evidence contains
only selected sanitized JSON, never secrets, databases, logs or backup archives.
