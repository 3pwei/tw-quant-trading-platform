#!/usr/bin/env bash
set -euo pipefail

MINUTES="${1:-60}"
[[ "${MINUTES}" =~ ^[0-9]+$ && "${MINUTES}" -ge 30 && "${MINUTES}" -le 360 ]]
INSTALL_ROOT="${INSTALL_ROOT:-/srv/trading-platform-staging}"
[[ "${INSTALL_ROOT}" == /srv/trading-platform-staging && "${INSTALL_ROOT}" != *production* ]]
BUNDLE="${INSTALL_ROOT}/bundle"
COMPOSE_ENV="${INSTALL_ROOT}/config/compose.env"
RELEASE_ENV="${INSTALL_ROOT}/deployments/active-release.env"
COMPOSE_FILE="${BUNDLE}/docker-compose.yml"
compose=(docker compose --env-file "${COMPOSE_ENV}" --env-file "${RELEASE_ENV}" -f "${COMPOSE_FILE}")

"${BUNDLE}/verify.sh" verify
record_before="$(sha256sum "${INSTALL_ROOT}/deployments/current.env" | cut -d' ' -f1)"
declare -A restarts
for service in market-api execution-worker gateway; do
  container_id="$("${compose[@]}" ps -q "${service}")"
  restarts["${service}"]="$(docker inspect --format '{{.RestartCount}}' "${container_id}")"
done

deadline=$(( $(date +%s) + MINUTES * 60 ))
samples=0
while (( $(date +%s) < deadline )); do
  for service in market-api execution-worker gateway; do
    container_id="$("${compose[@]}" ps -q "${service}")"
    [[ -n "${container_id}" ]]
    state="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "${container_id}")"
    [[ "${state}" == healthy || "${state}" == running ]]
    [[ "$(docker inspect --format '{{.RestartCount}}' "${container_id}")" == "${restarts[${service}]}" ]]
  done
  health="$("${compose[@]}" exec -T execution-worker cat /run/tw-quant-execution/health.json)"
  python -c 'import json,sys; d=json.load(sys.stdin); assert d["locked"] is True; assert d["external_order_calls"] == 0; assert d["external_cancel_calls"] == 0; assert not d.get("reconciliation_in_progress", False)' <<<"${health}"
  curl --fail --silent http://127.0.0.1:18080/healthz >/dev/null
  samples=$((samples + 1))
  sleep 10
done

[[ "$(sha256sum "${INSTALL_ROOT}/deployments/current.env" | cut -d' ' -f1)" == "${record_before}" ]]
for service in market-api execution-worker gateway; do
  logs="$("${compose[@]}" logs --no-color --since "${MINUTES}m" --tail 2000 "${service}")"
  if grep -Eqi '(traceback|secret[=:][^[:space:]]+|token[=:][^[:space:]]+|credential[=:][^[:space:]]+)' <<<"${logs}"; then
    echo "Soak log policy failed for ${service}" >&2
    exit 1
  fi
done
"${compose[@]}" exec -T market-api python - <<'PY'
import os, sqlite3
path = os.environ.get("MARKET_DB_PATH", "/data/staging.sqlite3")
connection = sqlite3.connect(path)
try:
    assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
finally:
    connection.close()
PY
"${BUNDLE}/verify.sh" verify
echo "P8_STAGING_SOAK=PASS minutes=${MINUTES} samples=${samples}"
