# Security exception re-review — 2026-10-24

Baseline: master `0c5f08055daa7a30b1dd9c275db78e1166f0ca0c` (PR #65).
This review follows the expiry failures through 2026-10-23. It does not renew
any exception, approve a release, or change real-order/deployment controls.

## Findings and evidence boundaries

The [2026-10-23 expiry run](https://github.com/3pwei/tw-quant-trading-platform/actions/runs/39919552496)
evaluated all nine entries, synchronized Issue #22 successfully (`unchanged`),
then failed the independent expiry enforcement step. The issue's October 18
evaluation date is the deduplicated notification snapshot, not a failed daily
evaluation. All deadlines remain 2026-10-18.

The latest scheduled Security run on this exact master was
[39226609018, 2026-10-19](https://github.com/3pwei/tw-quant-trading-platform/actions/runs/39226609018).
Its unfiltered image evidence found all eight Debian IDs in both market-api and
execution-worker, with no fixed version reported. Gateway had no HIGH/CRITICAL
findings. These are October 19 image observations, not a new October 24 scan or
an inspection of the running Production host. This workspace has no Docker or
Trivy executable; current image build/scan/SBOM checks require CI.

## Disposition of the original nine exceptions

Installed versions below come from that unfiltered scan. Debian status was
rechecked against each linked official tracker on October 24. Source-package
findings can cover multiple binary packages; that alone does not establish
reachability in the application.

| Advisory | Installed package/version | Current upstream disposition | Action |
| --- | --- | --- | --- |
| [GHSA-vfj7-8cjw-p6xm](https://github.com/advisories/GHSA-vfj7-8cjw-p6xm) | braces 3.0.3, dev-only | GitHub withdrew it on 2026-10-23 as not practically exploitable; no patched release | Remove stale npm exception; no braces upgrade or claim of code remediation |
| [CVE-2025-69720](https://security-tracker.debian.org/tracker/CVE-2025-69720) | ncurses binaries 6.5+20250216-2 | trixie vulnerable/no-dsa; unstable fixed at 6.6+20251231-1 | Human risk review; no verified stable upgrade |
| [CVE-2026-16742](https://security-tracker.debian.org/tracker/CVE-2026-16742) | libsystemd0, libudev1 257.13-1~deb13u1 | trixie vulnerable/no-dsa; unstable fixed at 261.2-1 | Human risk review; no verified stable upgrade |
| [CVE-2026-54369](https://security-tracker.debian.org/tracker/CVE-2026-54369) | libacl1 2.3.2-2+b1 | trixie vulnerable/no-dsa; 2.4.0-1 fixed in unstable; stable point release still pending | Human risk review; do not mix unstable ABI into stable image |
| [CVE-2026-76642](https://security-tracker.debian.org/tracker/CVE-2026-76642) | util-linux family 2.41.5-0+deb13u1 | trixie vulnerable/no-dsa; unstable fixed at 2.42.3-1 | Human risk review; no verified stable upgrade |
| [CVE-2026-78408](https://security-tracker.debian.org/tracker/CVE-2026-78408) | util-linux family 2.41.5-0+deb13u1 | trixie vulnerable/no-dsa; unstable fixed at 2.42.4-1 | Human risk review; no verified stable upgrade |
| [CVE-2026-78409](https://security-tracker.debian.org/tracker/CVE-2026-78409) | util-linux family 2.41.5-0+deb13u1 | trixie vulnerable/no-dsa; unstable fixed at 2.42.3-1 | Human risk review; no verified stable upgrade |
| [CVE-2026-78410](https://security-tracker.debian.org/tracker/CVE-2026-78410) | util-linux family 2.41.5-0+deb13u1 | trixie vulnerable/no-dsa; unstable fixed at 2.42.3-1 | Human risk review; no verified stable upgrade |
| [CVE-2026-9538](https://security-tracker.debian.org/tracker/CVE-2026-9538) | perl-base 5.40.1-6+deb13u1 | trixie vulnerable/postponed for upstream regressions; unstable fixed at 5.42.3-1 | Human risk review; no verified stable upgrade |

No original entry is proven fixed by an installed-version upgrade. One is
obsolete because its advisory was withdrawn; eight remain unresolved. A newer
floating base tag does not prove remediation. Keep the pinned Python base and
existing exact signed stable backports until a tested stable fix is available.

### Evidence still required for human risk decisions

The source specifies non-root runtime users, read-only containers, dropped
capabilities and `no-new-privileges`. These reduce exposure but do not remove
the package findings or authorize renewed deadlines. Verify the built image
and deployed configuration independently before making a non-exploitability
claim. Specifically review:

- ncurses: presence/invocation of `infocmp` and attacker-controlled terminfo.
- systemd: presence/running state of systemd-homed and homed-managed users;
  the scan named libraries, not proof that the vulnerable service is running.
- acl: privileged pathname-based ACL callers and attacker-writable symlink
  components; the new upstream ABI requires compatibility review.
- util-linux: SUID mount/helpers, authorized fstab hooks, namespace/cgroup
  access and privileged `nsenter --join-cgroup` use, including host exposure.
- perl: whether Archive::Tar is installed/reachable and handles untrusted tar
  files; a perl-base source finding does not prove module availability.

Any future time-bounded renewal needs a separate explicit review with current
image IDs, scanner/DB versions, exploitability evidence, mitigations, owner and
deadline. No such renewal is included here. No package database deletion,
blanket allowlist removal, severity relaxation, or unstable package mixing is
used to make a gate pass.

## Additional dependency findings and minimal fix

A fresh complete npm audit on the baseline no longer returned the withdrawn
braces advisory. It did report three newer Next.js advisories against locked
16.3.8, each officially fixed in 16.4.1:

| Advisory | Severity | Fix |
| --- | --- | --- |
| [GHSA-2464-fhr4-w9xw](https://github.com/advisories/GHSA-2464-fhr4-w9xw) | Critical | next 16.4.1 |
| [GHSA-p3m5-59mp-jv5r](https://github.com/advisories/GHSA-p3m5-59mp-jv5r) | High | next 16.4.1 |
| [GHSA-673q-mjm6-r5xr](https://github.com/advisories/GHSA-673q-mjm6-r5xr) | Critical | next 16.4.1 |

Raise only the Next.js minimum to `^16.4.1` and lock it and its nine @next
runtime/compiler packages to 16.4.1. No packages are added/removed. React,
eslint-config-next 16.3.8, braces 3.0.3, micromatch 4.0.8 and sharp 0.35.5 stay
unchanged. Existing lint/build tests validate this smaller change; a broad
toolchain upgrade is unnecessary. The gateway copies static `out/` files into
Caddy rather than running Next.js; that limits runtime exposure but does not
exempt the dependency from the audit policy.

Re-read all ten changed public npm archives, verify their lockfile integrity,
and refresh versioned license/NOTICE inventory and input digests. Retain exact
new license texts and all existing texts. Update the public source manifest.

The October 19 Python audit failed before completing its vulnerability check:
`shioaji==1.7.4` could not be resolved from PyPI. This is an independent SDK
availability blocker, not one of the nine exceptions or evidence of a clean
Python audit. SDK/lock/capability changes require separate compatibility work;
no SDK upgrade, audit exclusion or replacement wheel is included here.

## Validation and release conditions

Local validation of the proposed change:

- Fresh full `npm audit` and the existing enforcement gate: HIGH=0,
  CRITICAL=0, exceptions=0. Returning advisories still fail the gate.
- Dashboard lint, 45 behavior tests, six blocking JS/TS rules and suppression
  rejection, security lint, TypeScript and static production build: PASS.
- Security exception/npm policy tests and publication/source verification:
  30 tests: 29 PASS, one environment-dependent Bandit integration skip.
  Public source/digests/dependency closure: PASS. Offline P9 regressions:
  139 tests, 138 PASS and one environment-dependent skip.
- Combined expiry evaluation still reports eight expired Trivy entries and
  enforcement must fail. Issue #22 remains open; this change cannot honestly
  make the overall Security workflow green.

Before merge/release, require exact-head CI (Python 3.11/3.12/3.14, Dashboard,
public image/Compose and offline P8/P9 regressions) and Security (expiry policy,
Python/npm audit, Bandit, JS/TS lint, both CodeQL languages, Gitleaks,
HIGH/CRITICAL image scans with `ignore-unfixed: false`, unfiltered evidence and
all three SBOMs). Resolve outstanding failures rather than bypassing gates.
CI's offline public-fixture tests are not a Candidate or staging deployment.

Candidate #31, P8 #25 and the P9 binding remain historical acceptance for their
recorded identities. They do not validate these changed dependencies or grant
deployment permission. No merge, rerun, workflow_dispatch, Candidate, P8 or
Production deployment is performed by this review.
