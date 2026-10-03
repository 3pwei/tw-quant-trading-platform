# P8 immutable staging and rollback

P8 is a staging-only delivery boundary. The approved application source remains
Platform `7d490254130d6fd3da5f8f88d9903cb1a35a2f88`, Core v1.2.0 wheel SHA-256
`63645e42755068c308d66d74ded5133395dfef816360dc8106e0bbc247ee49bd`,
private provider v0.2.0 wheel SHA-256
`a5cbd8147bfd517e0297f6e70fdbbf33b63c22ab58e570a35bfdbdfc26993ee5`,
and P7 acceptance `97ba63f514c6adeb531666e9b10d8b05578cde76`.

`P8 Staging Candidate` is a manual master workflow. It verifies exact CI and
Security gates, materializes the approved Platform source with `git archive`,
downloads the private wheel only inside the controlled runner, verifies its
bytes, and injects it with a BuildKit secret mount. The read token is neither a
build argument nor an image file. The Public runtime image remains provider-free.
Two distinct runtime images (known-good A and candidate B) and one gateway image
are each built once, exercised, scanned, assigned CycloneDX SBOMs, and only then
pushed. The manifest binds registry digest, image configuration digest, source,
pipeline, Core, provider, P7 and configuration identities.

`P8 Deploy Staging` accepts only one successful candidate workflow run ID. It
checks out that manifest's exact pipeline revision, uses a separate `staging`
GitHub Environment and staging-only SSH credentials, and pulls exact digest
references. The host Compose file contains no `build:` section and uses
`pull_policy: never`; deployment cannot rebuild or mutate the artifact.

The staging root is fixed at `/srv/trading-platform-staging`. The Compose project,
SQLite volume, health volume, configuration files and provider factory file are
staging-specific. The execution worker has `network_mode: none`, no broker secret
mount, no port and all live/read-only/canary/auto/Guardian switches disabled. The
gateway binds only to host loopback port 18080. Data is deterministic synthetic
input. All host scripts reject a Production-named or non-staging install root.

The drill is fail-closed:

1. deploy and verify known-good A;
2. deploy and verify candidate B;
3. rollback from the atomic `previous.env` record to A without rebuilding;
4. restart and verify exact running image/configuration identities;
5. soak the rolled-back known-good release for 30–360 minutes.

The current record is updated only after runtime and restart acceptance. Failed
deployment restores the prior immutable images and leaves the known-good record
unchanged. The soak fails on health loss, crash/restart drift, record drift,
unresolved execution state, SQLite integrity failure, suspicious credential log
output or any non-zero external order/cancel count.

Runtime acceptance also executes the reviewed broker, execution worker, canary,
Guardian, reconciliation and recovery contract tests in an ephemeral container
from the same approved runtime digest. It has no network, a read-only rootfs and
only the tests bind-mounted read-only; tests are not added to the image. This
exercises locked/disabled execution, Kill Switch, ARM, UNKNOWN ambiguity and
durable order/outbox-before-submit behavior on the staging artifact bytes.

This work does not authorize Production deployment, Production database access,
real broker credentials, real orders, or P9.
