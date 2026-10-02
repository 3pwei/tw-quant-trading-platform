# Generic deployment and rollback policy

These templates describe mechanisms, not an installed host or authorization to
deploy. Keep actual host/user, DNS/TLS identity, external environment, credentials
and runtime records outside Git. The example prefix `/srv/trading-platform` has
no association with an existing installation.

Only workflow_dispatch on master may request one exact lowercase 40-character
commit SHA with `DEPLOY lightsail-production <exact SHA>` confirmation. CI and
Security push runs for that exact master revision must succeed. Missing, pending,
skipped, failed or newer failed attempts cannot authorize it. No push or
Security-success trigger deploys. Environment approvals are additional protection
and never replace these guards. External SSH variables/secrets provide host/user,
key and verified host key; host-key verification remains mandatory. The approved
Public revision is staged as a verified Git bundle without host Git credentials.

Bootstrap requires an explicitly supplied Public repository URL, prepares tools
and external example configs, and never starts containers. Provision production
auth/RBAC and TLS in owner-only external files. Live/auto/canary/Guardian remain
disabled. For isolated replay, run `python -m tw_quant.synthetic_data --output
<runtime path>` and set MARKET_REPLAY_CSV accordingly. No market CSV or database
payload is published. Strategy operations require a separately injected provider.

Before replacement, validate auth and locked execution config/secret permissions.
Preserve backup integrity, non-root, read-only rootfs, ALL capability drop,
no-new-privileges, bounded resources, execution isolation and no execution port.
Verify exact running image/revision/Core identity, start/restart, gateway TLS/SNI
and independent external public health/headers. Localhost health is insufficient.

Before forward deployment, verify the current host revision against its existing
atomic owner-only deployment record (directory 700/file 600) and successful exact
CI/Security gates. Validate the known-good SHA and its Public ancestry. A host
without a verified record cannot enter rollback-enabled deployment: establish
the first baseline in a separately authorized staging/host procedure. No fake
baseline or historical revision is supplied. Failed forward/public verification
restores the retained verified revision under the lock and verifies record/public
health. Rollback keeps the run failed and never enables Live. Database restore
and schema compatibility require separate review; no implicit row restoration.

Diagnostics expose bounded service state and redacted output; never dump
container environment or broker identity. Actual deployment/rollback drills are
separately authorized operations.
