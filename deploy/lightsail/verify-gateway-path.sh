#!/usr/bin/env bash
set -euo pipefail

INSTALL_ROOT="${INSTALL_ROOT:-/srv/trading-platform}"
GATEWAY_ENV="${INSTALL_ROOT}/config/gateway.env"
temporary="$(mktemp -d)"
trap 'rm -rf "${temporary}"' EXIT

domain="$(sed -n 's/^MARKET_DOMAIN=//p' "${GATEWAY_ENV}" | tail -n 1)"
if [[ ! "${domain}" =~ ^[A-Za-z0-9.-]+$ || "${domain}" == localhost || "${domain}" == *..* ]]; then
  echo "Gateway hostname configuration is invalid" >&2
  exit 2
fi

status="$(curl --silent --show-error --output "${temporary}/body" \
  --dump-header "${temporary}/headers" --write-out '%{http_code}' \
  --connect-timeout 5 --max-time 10 --noproxy '*' \
  --resolve "${domain}:443:127.0.0.1" \
  "https://${domain}/healthz")"
[[ "${status}" == 200 ]]
[[ "$(cat "${temporary}/body")" == ok ]]
grep -qi '^Content-Security-Policy:' "${temporary}/headers"
grep -qi '^Permissions-Policy:' "${temporary}/headers"
grep -qi '^Cross-Origin-Opener-Policy: same-origin' "${temporary}/headers"
grep -qi '^Cross-Origin-Resource-Policy: same-origin' "${temporary}/headers"
grep -qi '^X-Content-Type-Options: nosniff' "${temporary}/headers"
echo "Gateway server-side health and security-header verification passed."
