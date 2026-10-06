# Manual Production READ-ONLY preflight

`Production READ-ONLY Preflight` is a separate `workflow_dispatch` workflow.
Creating or merging its PR does not authorize dispatch. It never invokes or
changes `P9 Production Cutover`, provisions prerequisites, or changes runtime
images. P8 stays complete; P10 and real-order execution remain disabled.

A future separately authorized dispatch must use master, successful latest
exact-master CI and Security, and environment `lightsail-production`. The job
uses the shared semantic Production SSH vars/secrets, config approval JSON and sealed
rollback SHA256 documented in `p9-production-cutover.md`. Hostnames, addresses,
SSH user and credentials are never evidence fields. SSH uses the reviewed strict
host-key and Production/Staging DNS-address isolation validation. SSH files are
created only in runner temporary storage and cleaned up on every outcome.

The first host check observes Legacy HEAD exactly
`683bb4ebc4c4980480a4786136701ff458338a14`, then identifies the provider from the
**running** Legacy market container environment, following its reviewed
`MARKET_DATA_PROVIDER` / `MARKET_MODE` compatibility default. A Shioaji, broker SDK
or unsupported provider stops immediately with `P9_PREREQUISITE=BLOCKED` and
`reason=accepted-runtime-market-capability`, before reading P9 files or requiring
config/rollback approvals. Do not switch Production to replay to pass: a new
broker-capable Candidate and P8 acceptance cycle is required.

For replay/mock, repeat the first checks under the already-existing Legacy deploy
lock, opened read-only with a nonblocking shared flock. Missing/busy lock blocks;
the workflow never creates one. Then inspect independent Production config
files/hashes, execution flags and fresh locked zero-call heartbeat, the physical
market/worker DB boundary, and the complete sealed rollback inventory. Reuse only
an explicit selection of reviewed P9 validation functions; transaction, restart,
healthcheck execution and upload functions are excluded from the host program.
Verify archived image config digests against an in-memory stream of each current
local image. No new archive, backup, image pull/build, container exec or lifecycle
operation is performed. Recheck identities, config/record hashes and SQLite
continuity before PASS.

Production SQLite files are never opened by SQLite itself. Read stable main/WAL
bytes into RAM, validate WAL checksums/salts, apply only committed frames, and
deserialize into `:memory:` with query-only and memory-only temporary storage.
Run integrity/locked-target checks and compare every logical table/schema digest
with the independently sealed backup. Concurrent file changes, rollback journals,
invalid or partially written/reused WAL tails, unlocked/NULL targets, separate DBs
or stale backups block without retry or repair. Each input file is bounded at
128 MiB; exceeding the bound blocks. This is stricter than `mode=ro`, which can
create/update a disk shared-memory sidecar. No checkpoint or backup operation is
used. No application code is imported in a running container.

The host executes reviewed Python from SSH stdin with `-B`; there is no source
bundle upload, remote directory creation or bytecode write. Host subprocesses are
allowlisted git identity, Docker inspect/list and existing-image stream reads.
Only schema-validated, sanitized evidence is printed and uploaded from the runner:
status/reason, revision/provider, file SHA256s, safe execution booleans and counters,
SQLite integrity/target counts, exact container/image/config identities and
rollback digests, and isolation results. Raw env, factory contents, DB rows, logs,
account identity, SSH data, tokens and exception details are never emitted.

BLOCKED fails the job and still publishes its sanitized evidence when available.
PASS reports that existing prerequisites satisfy this inspection; it does not
authorize or dispatch cutover. Review regression tests and exact-tree verification
before merging; dispatch and Production access require separate authorization.
