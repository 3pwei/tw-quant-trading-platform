#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE_FILE="${ROOT}/deploy/lightsail/docker-compose.yml"
TEMP_DIR="$(mktemp -d)"
MARKET_ENV="${TEMP_DIR}/market.env"
GATEWAY_ENV="${TEMP_DIR}/gateway.env"
EXECUTION_ENV="${TEMP_DIR}/execution.env"
SECRETS_DIR="${TEMP_DIR}/secrets"
COMPOSE=(docker compose -f "${COMPOSE_FILE}")

sanitize() {
  sed -E \
    -e 's/((KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)[A-Z0-9_]*[=:])[[:space:]]*[^[:space:]]+/\1[REDACTED]/Ig' \
    -e 's/([A-Za-z0-9._%+-]+)@[A-Za-z0-9.-]+/redacted@example.invalid/g'
}

diagnostics() {
  echo "Sanitized hardened-container diagnostics" >&2
  "${COMPOSE[@]}" ps >&2 || true
  for service in market-api gateway; do
    container_id="$("${COMPOSE[@]}" ps -aq "${service}" 2>/dev/null || true)"
    [[ -n "${container_id}" ]] || continue
    docker inspect --format \
      'service={{index .Config.Labels "com.docker.compose.service"}} status={{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} uid={{.Config.User}} readonly={{.HostConfig.ReadonlyRootfs}}' \
      "${container_id}" 2>&1 | sanitize >&2 || true
    docker inspect --format '{{if .State.Health}}{{range .State.Health.Log}}{{.End}} exit={{.ExitCode}} {{.Output}}{{println}}{{end}}{{end}}' \
      "${container_id}" 2>&1 | tail -n 40 | sanitize >&2 || true
    docker logs --tail 100 "${container_id}" 2>&1 | sanitize >&2 || true
  done
}

cleanup() {
  status=$?
  if (( status != 0 )); then
    diagnostics
  fi
  "${COMPOSE[@]}" down --volumes --remove-orphans >/dev/null 2>&1 || true
  rm -rf "${TEMP_DIR}"
  exit "${status}"
}
trap cleanup EXIT

cp "${ROOT}/deploy/lightsail/market.env.example" "${MARKET_ENV}"
cat >>"${MARKET_ENV}" <<'EOF'
MARKET_ALLOWED_ORIGINS=http://127.0.0.1
MARKET_ACCESS_MODE=disabled
PLATFORM_ENVIRONMENT=development
PLATFORM_AUTHORIZATION_MODE=disabled
PLATFORM_BOOTSTRAP_ADMIN_EMAILS=
EOF

cp "${ROOT}/deploy/lightsail/gateway.env.example" "${GATEWAY_ENV}"
cat >>"${GATEWAY_ENV}" <<'EOF'
MARKET_DOMAIN=http://:80
ACME_EMAIL=redacted@example.invalid
EOF

cp "${ROOT}/deploy/lightsail/execution.env.example" "${EXECUTION_ENV}"
mkdir -m 0700 "${SECRETS_DIR}"

export MARKET_ENV_FILE="${MARKET_ENV}"
export GATEWAY_ENV_FILE="${GATEWAY_ENV}"
export EXECUTION_ENV_FILE="${EXECUTION_ENV}"
export EXECUTION_SECRETS_DIR="${SECRETS_DIR}"
export DEPLOY_COMMIT_SHA="${GITHUB_SHA:-ci-runtime-regression}"

"${COMPOSE[@]}" build market-api gateway

prepare_volume() {
  local volume="$1" uid="$2" gid="$3" mode="$4" mountpoint
  docker volume create "${volume}" >/dev/null
  mountpoint="$(docker volume inspect --format '{{.Mountpoint}}' "${volume}")"
  sudo chown -R "${uid}:${gid}" "${mountpoint}"
  sudo chmod "${mode}" "${mountpoint}"
}

prepare_volume platform-production_market-data 10001 10001 0750
prepare_volume platform-production_execution-health 10001 10001 0750
prepare_volume platform-production_caddy-data 10000 10000 0750
prepare_volume platform-production_caddy-config 10000 10000 0750

"${COMPOSE[@]}" run --rm --no-deps -T market-api python -m tw_quant.synthetic_data --output /data/synthetic.csv

"${COMPOSE[@]}" up --no-build --detach --wait --wait-timeout 120 market-api gateway

wait_healthy() {
  local service="$1" container_id state
  for _ in {1..60}; do
    container_id="$("${COMPOSE[@]}" ps -q "${service}")"
    state="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "${container_id}")"
    [[ "${state}" == healthy || "${state}" == running ]] && return 0
    [[ "${state}" == unhealthy || "${state}" == exited || "${state}" == dead ]] && return 1
    sleep 2
  done
  return 1
}

wait_healthy market-api
wait_healthy gateway

market_id="$("${COMPOSE[@]}" ps -q market-api)"
gateway_id="$("${COMPOSE[@]}" ps -q gateway)"

[[ "$(docker exec "${market_id}" id -u)" == "10001" ]]
[[ "$(docker inspect --format '{{.HostConfig.ReadonlyRootfs}}' "${market_id}")" == "true" ]]
[[ "$(docker inspect --format '{{json .HostConfig.CapDrop}}' "${market_id}")" == *ALL* ]]
[[ "$(docker inspect --format '{{json .HostConfig.SecurityOpt}}' "${market_id}")" == *no-new-privileges:true* ]]
[[ "$(docker exec "${market_id}" sh -c 'printf "%s" "$HOME"')" == "/tmp" ]]
docker exec "${market_id}" sh -c 'touch /data/.runtime-write-test /tmp/.runtime-write-test'
! docker exec "${market_id}" sh -c 'touch /app/.must-stay-read-only' 2>/dev/null
[[ "$(docker exec "${market_id}" stat -c '%u:%g' /data)" == "10001:10001" ]]
[[ "$(docker exec "${gateway_id}" id -u)" == "10000" ]]
[[ "$(docker inspect --format '{{json .HostConfig.CapAdd}}' "${gateway_id}")" == *NET_BIND_SERVICE* ]]

status="$(curl --silent --show-error --output "${TEMP_DIR}/health" --write-out '%{http_code}' http://127.0.0.1/healthz)"
[[ "${status}" == "200" ]]
[[ "$(cat "${TEMP_DIR}/health")" == "ok" ]]
curl --silent --show-error --dump-header "${TEMP_DIR}/headers" --output /dev/null http://127.0.0.1/healthz
grep -qi '^Content-Security-Policy:' "${TEMP_DIR}/headers"
grep -qi '^Permissions-Policy:' "${TEMP_DIR}/headers"
grep -qi '^Cross-Origin-Opener-Policy: same-origin' "${TEMP_DIR}/headers"
grep -qi '^Cross-Origin-Resource-Policy: same-origin' "${TEMP_DIR}/headers"
grep -qi '^X-Content-Type-Options: nosniff' "${TEMP_DIR}/headers"

"${COMPOSE[@]}" restart market-api gateway
wait_healthy market-api
wait_healthy gateway
[[ "$(curl --silent --show-error http://127.0.0.1/healthz)" == "ok" ]]

echo "Hardened Market API and Gateway runtime regression passed."
