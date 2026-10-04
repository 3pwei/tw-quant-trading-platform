# Security exception review — 2026-10-04

Security PASS means the current findings satisfy the reviewed, expiring policy;
it does not mean there are no known vulnerabilities. No expiry is extended by
this review. Remaining container and npm exceptions still expire on 2026-10-18.

## Stable fixes and removed exceptions

Both public and private-composition runtime Dockerfiles keep the same base digest
and signed Debian stable repositories. They install and verify exact backport
versions; an unavailable version fails the build. No unstable repository, source
patch or floating upgrade is introduced.

| Source package | Installed binary/version | Removed CVEs |
| --- | --- | --- |
| sqlite3 | libsqlite3-0 3.46.1-7+deb13u2 | CVE-2026-11822, CVE-2026-11824 |
| perl | perl-base 5.40.1-6+deb13u1 | CVE-2026-13221, CVE-2026-42496, CVE-2026-42497, CVE-2026-48962, CVE-2026-57432, CVE-2026-57433, CVE-2026-8376 |
| gzip | gzip 1.13-1+deb13u1 | CVE-2026-41992 |
| pcre2 | libpcre2-8-0 10.46-1~deb13u3 (already pinned) | CVE-2026-86145, CVE-2026-89157, CVE-2026-89161 |

Upstream evidence: Debian's [sqlite3](https://security-tracker.debian.org/tracker/source-package/sqlite3),
[perl](https://security-tracker.debian.org/tracker/CVE-2026-13221),
[gzip](https://security-tracker.debian.org/tracker/CVE-2026-41992) and
[pcre2](https://security-tracker.debian.org/tracker/CVE-2026-86145) trackers.
Every removed CVE has its own page at `https://security-tracker.debian.org/tracker/<CVE>`.
The image scans, rather than upstream status alone, must pass with these 13 IDs
removed from the allowlist. Existing SQLite persistence/recovery and complete
deployment/Compose tests exercise the patched public runtime. Private composition
and staging require a new separately authorized Candidate; these are not run here.

## Retained exceptions

The Debian tracker still lists these stable (trixie) packages as vulnerable at
review time. These are outstanding exceptions, not claims of non-exploitability.
Each ID links to the upstream status and rationale; re-evaluate before expiry.

| Package | Retained CVEs | Upstream status |
| --- | --- | --- |
| ncurses | [CVE-2025-69720](https://security-tracker.debian.org/tracker/CVE-2025-69720) | no-dsa |
| systemd | [CVE-2026-16742](https://security-tracker.debian.org/tracker/CVE-2026-16742) | no-dsa |
| acl | [CVE-2026-54369](https://security-tracker.debian.org/tracker/CVE-2026-54369) | pending point release |
| util-linux | [CVE-2026-76642](https://security-tracker.debian.org/tracker/CVE-2026-76642), [CVE-2026-78408](https://security-tracker.debian.org/tracker/CVE-2026-78408), [CVE-2026-78409](https://security-tracker.debian.org/tracker/CVE-2026-78409), [CVE-2026-78410](https://security-tracker.debian.org/tracker/CVE-2026-78410) | no-dsa |
| perl | [CVE-2026-9538](https://security-tracker.debian.org/tracker/CVE-2026-9538) | postponed pending upstream regressions |

The [braces advisory](https://github.com/advisories/GHSA-vfj7-8cjw-p6xm)
lists no patched release; the npm registry still reports 3.0.3 as latest. Keep
the exact dev-only exception and lock validation. Its five affected packages are
one advisory/dependency chain, not five independent CVEs. It is not shipped in
the static gateway runtime. No forced dependency downgrade or lint removal is
used to hide the finding.

## Review evidence

Security retains `runtime-vulnerability-evidence` for 14 days: unfiltered
HIGH/CRITICAL Trivy JSON for both public images, their exact local image IDs and
scanner/database version information. These diagnostic scans deliberately use
no allowlist and do not suppress unfixed findings. Their finding exit code is
zero so the full report is retained, but scanner errors still fail the step;
the original allowlisted blocking scans remain mandatory with exit code 1.
`npm-audit-evidence` retains the complete npm JSON, including on policy failure.
Artifacts are associated with the exact workflow run/commit, not checked into
the approved source manifest. They contain public dependency metadata only.
