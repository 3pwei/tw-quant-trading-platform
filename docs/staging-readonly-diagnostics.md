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
- A result PASS followed by collector timeout points beyond completed probe work.
- A successful diagnostic cannot retroactively explain P8 #18 or grant acceptance.

## Removal

After the root cause is fixed and full P8 acceptance succeeds, open a separate
cleanup PR deleting this workflow, `deploy/staging/readonly_diagnostics.py`, its
dedicated test and this document; update `public-candidate.json` accordingly.
Keep the reusable collector/probe diagnostics and regression tests from PR #55.
No diagnostic daemon is installed on Staging. Historical Actions records and
artifacts follow retention settings; removing YAML is not a history deletion.
