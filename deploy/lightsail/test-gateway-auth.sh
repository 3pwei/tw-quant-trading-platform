#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NETWORK="tw-quant-gateway-auth-$$"
ORIGIN="tw-quant-auth-origin-$$"
GATEWAY="tw-quant-auth-gateway-$$"
PORT=18081

cleanup() {
  docker rm --force "${GATEWAY}" "${ORIGIN}" >/dev/null 2>&1 || true
  docker network rm "${NETWORK}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker network create "${NETWORK}" >/dev/null
docker run --detach --name "${ORIGIN}" --network "${NETWORK}" \
  --network-alias market-api --read-only --cap-drop ALL \
  --env DEMO_BACKTEST_ENABLED=true \
  --security-opt no-new-privileges \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m,uid=10001,gid=10001 \
  --mount "type=bind,source=${ROOT}/tests/gateway_auth_app.py,target=/app/gateway_auth_app.py,readonly" \
  --mount "type=bind,source=${ROOT}/tests/public_fixtures.py,target=/app/public_fixtures.py,readonly" \
  platform-market-api:ci \
  uvicorn gateway_auth_app:app --host 0.0.0.0 --port 8000 >/dev/null

origin_ready=false
for _ in {1..30}; do
  if docker exec "${ORIGIN}" python -c \
    "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=2)" \
    >/dev/null 2>&1; then
    origin_ready=true
    break
  fi
  sleep 1
done
[[ "${origin_ready}" == true ]] || { echo 'Origin did not become ready' >&2; exit 1; }

docker run --detach --name "${GATEWAY}" --network "${NETWORK}" \
  --read-only --cap-drop ALL --cap-add NET_BIND_SERVICE \
  --security-opt no-new-privileges \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=32m,uid=10000,gid=10000 \
  --tmpfs /data:rw,noexec,nosuid,nodev,size=32m,uid=10000,gid=10000 \
  --tmpfs /config:rw,noexec,nosuid,nodev,size=32m,uid=10000,gid=10000 \
  --env MARKET_DOMAIN=http://:8080 --env ACME_EMAIL=fixture@example.invalid \
  --publish "127.0.0.1:${PORT}:8080" platform-gateway:ci >/dev/null

BASE="http://127.0.0.1:${PORT}"
ready=false
for _ in {1..30}; do
  if [[ "$(curl --silent --output /dev/null --write-out '%{http_code}' "${BASE}/healthz")" == 200 ]]; then
    ready=true
    break
  fi
  sleep 1
done
[[ "${ready}" == true ]] || { echo 'Gateway did not become ready' >&2; exit 1; }

check_status() {
  local expected="$1"; shift
  local actual
  actual="$(curl --silent --output /dev/null --write-out '%{http_code}' "$@")"
  if [[ "${actual}" != "${expected}" ]]; then
    echo "Gateway authorization regression: expected ${expected}, received ${actual} (${!#})" >&2
    exit 1
  fi
}

# A forged registered subject and role cannot pass Caddy without an assertion.
check_status 401 -H 'X-Authenticated-Subject: fixture-admin' \
  -H 'X-Authenticated-Email: admin@example.com' \
  -H 'X-Authenticated-Role: admin' "${BASE}/api/admin/users"
check_status 401 -H 'Cf-Access-Jwt-Assertion: invalid' \
  -H 'X-Authenticated-Subject: fixture-admin' "${BASE}/api/admin/users"

# A verified researcher token wins over every spoofed forwarded identity header.
READER_HEADERS=(-H 'Cf-Access-Jwt-Assertion: fixture-reader' \
  -H 'X-Authenticated-Subject: fixture-admin' \
  -H 'X-Authenticated-Email: admin@example.com' \
  -H 'X-Authenticated-User-ID: forged-admin' \
  -H 'X-Authenticated-Role: admin')
check_status 403 "${READER_HEADERS[@]}" "${BASE}/api/admin/users"
check_status 200 "${READER_HEADERS[@]}" "${BASE}/api/me"
curl --fail --silent --show-error "${READER_HEADERS[@]}" "${BASE}/api/me" \
  | python3 -c 'import json,sys; u=json.load(sys.stdin); assert u["email"] == "reader@example.com" and u["role"] == "researcher"'
check_status 200 -H 'Cf-Access-Jwt-Assertion: fixture-admin' "${BASE}/api/admin/users"

# Public Demo is admitted by exact path and method only, with no trusted identity.
FORGED=(-H 'X-Authenticated-Subject: fixture-admin' \
  -H 'X-Authenticated-Role: admin' \
  -H 'Cf-Access-Authenticated-User-Email: admin@example.com' \
  -H 'X-Forwarded-For: 198.51.100.7')
check_status 308 "${BASE}/demo"
check_status 200 "${BASE}/demo/"
check_status 200 "${BASE}/favicon.svg"
ASSET="$(docker exec "${GATEWAY}" sh -c 'find /srv/_next/static -type f -name "*.js" -print -quit')"
[[ -n "${ASSET}" ]] || { echo "Next static asset missing" >&2; exit 1; }
check_status 200 "${BASE}${ASSET#/srv}"
check_status 200 "${BASE}/api/demo/cases"
check_status 200 "${FORGED[@]}" "${BASE}/api/demo/cases"
check_status 401 "${FORGED[@]}" "${BASE}/api/me"
for path in /backtest/ /strategies/ /history/ /settings/ /admin/users/ /trade/ \
  /api/backtest-runs /api/strategies /api/paper/orders /api/live/orders \
  /ws/market /docs /openapi.json /api/demo/cases/ /api/demo/backtests/ \
  /api/demo/unlisted; do
  check_status 401 "${FORGED[@]}" "${BASE}${path}"
done
check_status 401 -X POST "${BASE}/api/demo/cases"
check_status 401 -X GET "${BASE}/api/demo/backtests"
check_status 401 --head "${BASE}/demo/"
check_status 200 -X POST -H 'Content-Type: application/json' \
  --data '{"case_id":"inert-case"}' "${BASE}/api/demo/backtests"
check_status 200 -X POST -H 'Content-Type: application/json' \
  --data '{"case_id":"inert-case"}' "${BASE}/api/demo/backtests"
check_status 200 -X POST -H 'Content-Type: application/json' \
  --data '{"case_id":"inert-other"}' "${BASE}/api/demo/backtests"
# Two catalog requests and three backtests above consume five slots.
for _ in {1..5}; do
  check_status 200 -X POST -H 'Content-Type: application/json' --data '{"case_id":"inert-case"}' "${BASE}/api/demo/backtests"
done
check_status 429 -X POST -H 'Content-Type: application/json' \
  --data '{"case_id":"inert-case"}' "${BASE}/api/demo/backtests"

echo 'Caddy Demo and formal forward-auth boundary passed.'
