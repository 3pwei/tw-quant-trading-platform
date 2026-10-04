# Unified exception expiry notifications

The daily `Security exception expiry` workflow reads both `.trivyignore.yaml`
and `deploy/security/npm-audit-exceptions.json`. npm entries use the enforcement
gate's schema, exact package-version and dev-only lock validation; missing or
malformed input fails closed. The same combined check also runs in Security.

The report labels each entry `trivy` or `npm`. Equal advisory IDs in different
sources remain distinct in the report and notification fingerprint. Both use
14/7/3-day warning bands and fail on the expiry date. The daily check first
writes the combined report so expired entries can be notified; the independent
`if: always()` enforcement step fails even if issue synchronization fails.

One existing tracking issue is reused. Repeating an unchanged source/advisory/
expiry/urgency state sends no new comment. A changed state updates that issue;
when neither source needs attention, it is closed. A failed check removes any
old report before evaluation so stale PASS evidence cannot be reused.

This change does not extend deadlines, change severity policy, remove findings,
or automatically renew exceptions. Tests exercise CLI warning/expiry behavior,
missing/malformed files, version/scope drift, stale report rejection and mocked
notification lifecycle. No real notification is sent by the tests. Creating
this PR does not dispatch the scheduled workflow or deploy an environment.
