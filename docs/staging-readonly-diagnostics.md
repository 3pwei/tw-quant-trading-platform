# Temporary Staging readonly diagnostic

This manual workflow investigates P8 #18's durable-snapshot timeout. It is not a
deployment, a fix, P8 acceptance, or a continuous monitoring service. It needs no
new Candidate because it inspects the existing worker without replacing images.
Any later P8 deployment still needs its normal exact-revision Candidate binding.

## Activation and inputs

Merge the diagnostic code and this entry point only after review and authorization.
After fresh master CI and Security pass, separately authorize one dispatch of
`Staging READ-ONLY Diagnostics` on `master` with:

- `expected_sha`: the full reviewed master commit SHA.
- `confirmation`: `DIAGNOSE staging <expected_sha>`.

The workflow is manual only, uses the `staging` environment and the existing
`STAGING_HOST`, `STAGING_USER`, `STAGING_SSH_PRIVATE_KEY`, and `STAGING_SSH_HOST_KEY`.
It requires attempt 1, checks master twice and checks successful master push
CI/Security before SSH. Its `p8-staging` concurrency group serializes it with P8
deployment. There is no automatic retry; a new diagnostic needs a new authorization.
No Production credentials, registry token or private provider factory is used.

## Exact observation boundary

The runner sends the reviewed collector/probe and a restricted entry function in
memory to `ssh ... sudo -n python3 -B -`. It does not copy a bundle onto Staging.
The entry function checks the fixed Staging root, healthy worker, Compose project
and service labels, `/data` volume identity, network isolation, and fresh
disabled/locked execution health with zero external order/cancel calls.

It then executes each command once, stopping at the first failure:

1. `docker exec <verified-worker> python -c '<fixed readiness print>'`
2. `docker exec -i <verified-worker> python -` with that print on stdin.
3. `docker exec -i <verified-worker> python - snapshot` with the reviewed probe.

The existing collector keeps its 15-second per-command deadline. The SSH wrapper
has a 120-second deadline; the workflow job has a five-minute bound. There is no
container stop/start/restart, pull/build, SQL write/DDL, seed, config/Secrets
change, broker import, backup or acceptance-ledger write. Full `integrity_check`
and the existing readonly target validation/hash are retained. SQLite readers
can consume I/O and take read locks; normal WAL shared-memory coordination is not
bypassed with `immutable=1`. Anonymous collector output files on the host and
temporary runner SSH files are closed/removed on normal exit and handled timeout.

Killing a timed-out Docker CLI or SSH process does not guarantee that its remote
exec process has exited. Report timeout and stop; never restart the service or
retry automatically. This diagnostic does not change that existing boundary.

## Evidence and interpretation

Only fixed allowlisted records reach logs and the 30-day artifact
`staging-readonly-diagnostics/p8-readonly-diagnostics.json`. The report always has
`acceptance: false`, even when all probes pass. Raw stdout/stderr, errors,
environment, IDs/hostnames, SQL rows and private parameters are not published.
Completed stage records survive collector and transport timeouts where received.

Host start, case and completion markers use the collector's stderr stream.
Completion is checked using the exact ordered case, collector-PASS and
durable-PASS sequences, exactly one host start and success marker, no failure
records, and a zero remote exit status. The success marker need not be the last
merged line: SSH can deliver a stdout success marker before an earlier stderr
timing record, as observed in diagnostic run #2. This fixes that diagnostic
false failure only; it does not explain run #1's timeout or change any timeout,
SQLite/target check, retry policy or P8 acceptance requirement.

- Failure before a Python-import marker alone does not distinguish Docker, EOF,
  interpreter startup, or transport loss; use the preceding argv/stdin cases.
- A START without PASS/FAIL identifies the unfinished probe segment.
- `sqlite-integrity` retains the total integrity duration and result validation.
  Its nested `sqlite-integrity-execute` and `sqlite-integrity-fetchone` records
  separately time `execute("PRAGMA integrity_check")` and `fetchone()`. These are
  Python API call boundaries, not a claim that SQLite performs all work in one
  of them. The ordered PASS records are execute, fetchone, then integrity.
  If SQLite raises an error, FAIL records include its numeric extended
  `sqlite_errorcode` (or `unknown` if unavailable); category uses the primary
  code. No exception text, SQL result, path or row is emitted. An outer timeout
  preserves received START/PASS records but does not invent a SQLite error code.
  The 15-second collector deadline, SQL, readonly connection and acceptance
  conditions are unchanged; this only distinguishes where the probe stopped.
- A result PASS followed by collector timeout points beyond completed probe work.
- A successful diagnostic cannot retroactively explain P8 #18 or grant acceptance.

## Removal

After the root cause is fixed and full P8 acceptance succeeds, open a separate
cleanup PR deleting this workflow, `deploy/staging/readonly_diagnostics.py`, its
dedicated test and this document; update `public-candidate.json` accordingly.
Keep the reusable collector/probe diagnostics and regression tests from PR #55.
No diagnostic daemon is installed on Staging. Historical Actions records and
artifacts follow retention settings; removing YAML is not a history deletion.
# Linux resource samples during the probe

P8 #20 stopped after `sqlite-integrity-execute START` with a 15,003 ms
`durable-snapshot` timeout. That locates the unfinished Python API call; it
does not establish corruption, lock contention, CPU saturation or disk latency.

The collector now starts one separate, short-lived Python standard-library
observer alongside the actual Docker stdin snapshot. It reads Linux `/proc`,
PSI and cgroup v2 files. No monitoring package, service, `ptrace`, container
capability or database setting is installed or changed. The existing probe
command, SQL, readonly URI, 15-second subprocess deadline and acceptance gates
remain unchanged. Instrumentation itself consumes some resources and may alter
timing; sampling is evidence, not a performance-neutral experiment.

The probe emits a numeric PID/start-ticks handshake on its diagnostic stream.
The observer binds it to a host PID using the container init PID's namespace,
the inner PID and the process start ticks. It never selects by process name,
command line or the host Docker CLI PID. Missing, ambiguous or changed identity
produces `unknown` process fields; it cannot select a substitute process.

`P8_RESOURCE` records contain only these allowlisted numeric fields:

| Scope | Fields and interpretation |
| --- | --- |
| `probe` | Host/container PID, start ticks, state, user/system CPU ticks, rchar/wchar, read_bytes/write_bytes, syscr/syscw |
| `host` | Cumulative CPU ticks (including idle, iowait and steal separately), CPU/I/O PSI `some total` microseconds, and disk read/write sectors and I/O milliseconds for the DB filesystem device only |
| `cgroup` | Container-group CPU usage microseconds, throttle count/time, device read/write bytes, CPU/I/O PSI totals |
| `limits` | Clock ticks/second, leaf cgroup CPU quota/period, effective cpuset CPU count, memory/pid limits and device I/O limits |

Limits are read once. Zero denotes an explicit unlimited value for a limit;
`unknown` denotes absent, inaccessible or unsupported data. Leaf cgroup limits
do not describe every inherited ancestor constraint. Cgroup v1 is not decoded.
Disk fields are unavailable if the filesystem device cannot be resolved in
`/proc/diskstats`; they are not substituted with sums across unrelated disks.
I/O byte counters are not equivalent to physical device latency or proof of a
SQLite lock. CPU time in seconds is the tick delta divided by clock ticks/second.
PSI and counter deltas over each sample interval are more useful than absolute
cumulative totals. An `S` or `D` process state alone is not a root-cause verdict.

The observer samples approximately once per second for at most 15 seconds and
16 samples, with offsets on the collector's host monotonic clock (not the delayed
GitHub log delivery timestamps). Its output is capped to 32 KiB/64 fixed-schema lines. The original
probe result is preserved if sampling is unavailable. Completion/timeout wakes
the observer through a pipe; bounded cleanup can kill only the observer's own
process group (including its read-only metadata lookup), never a container or
the application. This change does not claim to solve a possibly surviving
container exec process after the existing Docker CLI timeout.

Samples are retained in P8 workflow logs, including on timeout. The read-only
diagnostic workflow also includes them in its existing JSON `stages` artifact.
They never count as required PASS markers or acceptance ledger events. There
are no environment values, SQL rows, process command lines, host device names,
cgroup paths or raw error messages in their output.

Local tests cover PID reuse/ambiguity, parser failures, missing metrics, output
sanitization, real subprocess completion and the real 15-second timeout. A
restricted local `/proc` may prevent real namespace lookup; CI must exercise
namespace-bound process counter sampling without that restriction. Remote
staging evidence requires separately authorized workflow execution after merge.
