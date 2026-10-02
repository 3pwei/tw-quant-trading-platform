# Public Trading Platform source candidate

Reviewed Platform orchestration, API/CLI/UI and safety tests depend only on the
immutable Core v1.2.0 distribution and public package registries. Concrete
strategy implementations/defaults must be supplied through externally injected
Core providers; missing providers fail closed. No concrete fallback exists.

Live/auto/canary/Guardian stay disabled. This source candidate does not authorize
repository creation, deployment, runtime data migration or private assembly.

## Validation

Python 3.11, 3.12 and 3.14: `uv lock --check`, then `uv sync --locked --extra
server --extra test`, `uv run --locked --extra server --extra test python
-m unittest discover -s tests -v`. Run the standalone source/digest checker
before and after installation: `python tools/verify_public_candidate.py` and
`uv run --locked --extra server --extra test python tools/verify_public_candidate.py
--runtime`. Dashboard: npm ci, npm test, npm run lint, npm run build.

[Generic deployment policy](docs/deployment.md) retains manual exact revision,
CI/Security, TLS, hardening and verified known-good rollback. Credentials and
runtime databases stay external. [Provenance](docs/provenance.md) records excluded
rights-unknown market/social payloads and deterministic runtime fixtures.
[Third-party notices](THIRD_PARTY_NOTICES.md) define source publication scope.
Optional SDK binary redistribution is excluded from the default Public images.

## P6 acceptance

P5 verifies source manifest and CodeQL policy only. After separately authorized
new Public root reconstruction, first CI, first Security, native CodeQL for both
Python and JavaScript/TypeScript, audits, Gitleaks and image Trivy/SBOM must pass.
A skipped/ineligible/failed CodeQL run cannot satisfy this gate; P7 stays blocked
until all P6 acceptance succeeds. The digest index grants no deployment authority.
