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

Staging #7 stopped at A's first image verification: the expected config digest
was compared with Docker's local image ID, which was the registry manifest
digest on that host. Image IDs are backend-specific (classic config ID versus
containerd manifest/index ID); they must not be used as portable config digests.
See Moby's `daemon/containerd/image_inspect.go` (`ID: target.Digest.String()`).
Candidate creation and staging verification now hash original config bytes from
the local `docker image save` stream. This works offline, writes no image/config
contents to disk or logs, and rejects missing, corrupt or ambiguous config data.
The running container ID must still match the locally resolved image ID, the
canonical RepoDigest must still match the approved registry reference, and the
config byte hash must independently match the candidate manifest. No check is
replaced with an OR between unrelated digest types.

PR CI uses isolated classic and containerd image stores to build a public
synthetic fixture, push it to a loopback registry, compare its registry config
descriptor, remove/re-pull it, and exercise the deployment verifier against a
real container. Both backends must reject a wrong config digest. Unit regressions
also reject running-ID drift, wrong repository digest and failed exports.
Staging #7 and Candidate #12 remain immutable evidence; this fix does not authorize
a rerun, merge, candidate creation or deployment. Because deployment checks out
the candidate's pipeline revision, Candidate #12 cannot pick up this verifier
fix. After review/merge and exact master gates, a newly authorized candidate is
required before another staging attempt. P8 remains BLOCKED pending acceptance.

Gateway routing uses mutually exclusive health, backend and static `handle`
blocks. Static `try_files` must stay inside the fallback block: Caddy sorts a
top-level rewrite before health responses and backend proxy matchers, which can
turn `/healthz` and API requests into HTTP 200 static error pages (Candidate #11).
Both the candidate smoke and Compose healthcheck require HTTP 200 and exactly
the two response bytes `ok`. The shared candidate/PR-CI smoke retains the
non-root, read-only, cap-drop-ALL and no-new-privileges boundary. On failure it
prints selected container state, bounded startup logs and a bounded hexadecimal
health body before cleanup; it never dumps the container environment.

PR CI builds the public staging gateway, runs that same container smoke, then
extracts its Caddy binary to exercise the real routing configuration with local
static fixtures and a mock backend. This covers API/query preservation, static
asset collisions, headers and WebSocket upgrade forwarding without private
provider credentials. Local routing tests accept `CADDY_BIN`; the CI step
always supplies it. A Caddyfile change requires a new candidate build and new
manifest; failed Candidate #11 must not be reused. Passing PR checks does not
authorize candidate creation, Staging deployment, Production or P9.

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

## Loopback ingress with preserved network isolation

Staging #8 reached A's first host HTTP probe, then failed with curl exit 7.
Read-only collection showed a requested Docker port binding but no effective
binding, while both the gateway-local health endpoint and its API upstream
returned HTTP 200. The gateway was attached only to the internal bridge. This
is evidence of a missing host ingress path, not a failed application health
endpoint. Do not assume `ports:` in a Compose source proves a published port.

The replacement keeps both gateway and API on the internal bridge and the
execution worker on `network_mode: none`. It does not add an external bridge,
host networking, or firewall exceptions to those containers. Caddy listens on
container loopback for its healthcheck, and on a filesystem Unix socket for
host ingress. The socket directory `/var/lib/tw-quant-staging-ingress` is owned
by UID/GID 10000 with mode 0700; the socket itself has mode 0600. This directory
contains no credentials and is the gateway's only new host mount.

The host's `p8-staging-ingress.socket` listens exclusively on 127.0.0.1:18080.
Socket activation passes that listening descriptor to the distribution's
`systemd-socket-proxyd`, which forwards bytes to the Unix socket. The service
runs as UID/GID 10000 without capabilities, with no-new-privileges and only
AF_UNIX socket creation. It cannot create outbound IP sockets. The TCP byte
forwarder also supports HTTP upgrade/WebSocket traffic. `prepare-host.sh`
requires systemd and its socket proxy before making changes, installs the two
reviewed units, and enables only the staging loopback socket. A failed bind is
fatal; it never terminates an existing listener or changes firewall rules.
The shared host/CI setup creates the locked `p8-staging-ingress` account with
UID/GID 10000, no home and a nologin shell. Container users do not create host
accounts; missing this host identity caused systemd `217/USER` in the new PR
gate. Setup rejects an existing conflicting name, UID or GID before changing
ownership. It never adopts another host user's identity or runs the proxy as root.
These host changes occur only during a separately authorized deployment.

The verifier requires a single internal gateway network, no Docker host port
bindings, the exact loopback systemd listener, and the Unix socket's type,
owner and permissions. Its bounded HTTP probe requires 200 and exactly `ok`
for `/healthz`, then HTTP 200 for `/health/live`. It distinguishes ingress from
upstream failure without logging response bodies or environment variables.
Soak uses the same probe, so it cannot accept a static error page as health.

Before scanning/publishing, Candidate runs `smoke_compose.py` on a disposable
systemd/Docker runner using both already-built runtime images and the same
gateway bytes. It uses the checked-in Compose and host environment templates,
installs the exact systemd units transiently, and exercises initial/start and
restart for A and B. It checks image IDs, UID, read-only rootfs, capabilities,
network isolation, locked execution/zero external calls and both host HTTP
paths. A healthy container without the host listener is a required negative
case. The proxy stays running across gateway recreation, exercising the stable
Unix socket path rather than retaining a stale container IP. No build or pull
occurs in this gate. Its temporary project/volumes/units are removed afterward;
it refuses an existing staging installation, ingress directory or ingress unit.

PR CI uses public images with an explicitly inert backend command override;
it does not access private artifacts. Candidate uses the real private runtime
composition with no backend override. These are distinct levels of evidence.
PR CI also requires real Caddy + systemd Unix socket tests for missing sockets,
restart, API and WebSocket forwarding. Workspaces that prohibit AF_UNIX can
explicitly skip that test locally; the skip is prohibited when CI is set.
Passing local HTTP tests is not a substitute for either mandatory CI gate.

Candidate #13 and Staging #8 must not be rerun to test these changed bytes or
pipeline files. The changes require review, exact master gates, a new Candidate
and separate Staging authorization. P8 remains BLOCKED. The separately found
failure-compensation environment bug and provenance/acceptance-evidence gaps
remain follow-up work; this ingress change does not claim to close that audit.

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
