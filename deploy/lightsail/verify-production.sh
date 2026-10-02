#!/usr/bin/env bash
set -euo pipefail

COMMIT_SHA="${1:?commit SHA is required}"
MODE="${2:-verify}"
INSTALL_ROOT="${INSTALL_ROOT:-/srv/trading-platform}"
REPOSITORY="${INSTALL_ROOT}/repo"
COMPOSE_FILE="${REPOSITORY}/deploy/lightsail/docker-compose.yml"
COMPOSE_ENV="${INSTALL_ROOT}/config/compose.env"
CORE_VERSION="1.2.0"
CORE_SHA256="63645e42755068c308d66d74ded5133395dfef816360dc8106e0bbc247ee49bd"

if [[ ! "${COMMIT_SHA}" =~ ^[0-9a-f]{40}$ ]]; then
  echo "Invalid commit SHA" >&2
  exit 2
fi

compose=(docker compose --env-file "${COMPOSE_ENV}" -f "${COMPOSE_FILE}")

wait_for_health() {
  local service="$1"
  local container_id state
  for _ in {1..30}; do
    container_id="$("${compose[@]}" ps -q "${service}")"
    if [[ -n "${container_id}" ]]; then
      state="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "${container_id}")"
      if [[ "${state}" == "healthy" || "${state}" == "running" ]]; then
        return 0
      fi
      if [[ "${state}" == "unhealthy" || "${state}" == "exited" || "${state}" == "dead" ]]; then
        echo "Container readiness failed" >&2
        return 1
      fi
    fi
    sleep 2
  done
  echo "${service} did not become ready" >&2
  return 1
}

verify_image_identity() {
  local service="$1"
  local container_id image_id revision core_version core_sha
  container_id="$("${compose[@]}" ps -q "${service}")"
  image_id="$(docker inspect --format '{{.Image}}' "${container_id}")"
  revision="$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "${image_id}")"
  core_version="$(docker image inspect --format '{{index .Config.Labels "io.tw-quant.core.version"}}' "${image_id}")"
  core_sha="$(docker image inspect --format '{{index .Config.Labels "io.tw-quant.core.sha256"}}' "${image_id}")"
  [[ "${revision}" == "${COMMIT_SHA}" ]]
  [[ "${core_version}" == "${CORE_VERSION}" ]]
  [[ "${core_sha}" == "${CORE_SHA256}" ]]
}

verify_container_security() {
  local service="$1" expected_uid="$2" expected_cap="$3"
  local container_id uid readonly no_new_privileges caps
  container_id="$("${compose[@]}" ps -q "${service}")"
  uid="$(docker exec "${container_id}" id -u)"
  readonly="$(docker inspect --format '{{.HostConfig.ReadonlyRootfs}}' "${container_id}")"
  no_new_privileges="$(docker inspect --format '{{json .HostConfig.SecurityOpt}}' "${container_id}")"
  caps="$(docker inspect --format '{{json .HostConfig.CapAdd}}' "${container_id}")"
  [[ "${uid}" == "${expected_uid}" ]]
  [[ "${readonly}" == "true" ]]
  [[ "${no_new_privileges}" == *'no-new-privileges:true'* ]]
  if [[ "${expected_cap}" == "none" ]]; then
    [[ "${caps}" == "null" || "${caps}" == "[]" ]]
  else
    [[ "${caps}" == *"${expected_cap}"* ]]
  fi
  [[ "$(docker inspect --format '{{json .HostConfig.CapDrop}}' "${container_id}")" == *'ALL'* ]]
  [[ "$(docker inspect --format '{{.HostConfig.PidsLimit}}' "${container_id}")" -gt 0 ]]
  [[ "$(docker inspect --format '{{.HostConfig.Memory}}' "${container_id}")" -gt 0 ]]
  [[ "$(docker inspect --format '{{.HostConfig.NanoCpus}}' "${container_id}")" -gt 0 ]]
}

verify_mount_boundaries() {
  local service="$1" container_id mounts
  container_id="$("${compose[@]}" ps -q "${service}")"
  mounts="$(docker inspect --format '{{range .Mounts}}{{println .Destination .RW}}{{end}}' "${container_id}")"
  case "${service}" in
    market-api)
      [[ "${mounts}" != *'/run/live-secrets'* ]]
      [[ "${mounts}" == *'/data true'* ]]
      [[ "${mounts}" == *'/run/tw-quant-execution false'* ]]
      ;;
    execution-worker)
      [[ "${mounts}" == *'/run/live-secrets false'* ]]
      [[ "${mounts}" == *'/run/tw-quant-execution true'* ]]
      [[ "$(docker exec "${container_id}" stat -c '%u:%g:%a' /run/live-secrets)" =~ ^10001:10001:(500|550|700|750)$ ]]
      ;;
    gateway)
      [[ "${mounts}" != *'/run/live-secrets'* ]]
      [[ "${mounts}" == *'/data true'* ]]
      [[ "${mounts}" == *'/config true'* ]]
      ;;
  esac
}

verify_runtime_dependency() {
  local service="$1"
  "${compose[@]}" exec -T "${service}" python - <<'PY'
import importlib.metadata
import tw_quant_core
from tw_quant.strategy_registry import get_strategy_services
get_strategy_services()
from tw_quant.broker.identity import BrokerAccountRef
from tw_quant.events.models import EventMetadata
from tw_quant.execution.position_ledger import PositionLedger
from tw_quant.market.models import KBar
from tw_quant.risk.engine import RiskConfig
from tw_quant_core.strategy import StrategyRuntime

assert importlib.metadata.version("tw-quant-core") == "1.2.0"
assert tw_quant_core.__version__ == "1.2.0"
for value in (
    BrokerAccountRef, EventMetadata, PositionLedger, KBar, RiskConfig, StrategyRuntime
):
    assert value.__module__.startswith("tw_quant_core."), value
PY
}

for service in market-api execution-worker gateway; do
  wait_for_health "${service}"
done
verify_container_security market-api 10001 none
verify_container_security execution-worker 10001 none
verify_container_security gateway 10000 NET_BIND_SERVICE
for service in market-api execution-worker gateway; do
  verify_mount_boundaries "${service}"
done
for service in market-api execution-worker; do
  verify_image_identity "${service}"
  verify_runtime_dependency "${service}"
done

# The execution healthcheck is the fail-closed contract: unless an explicitly
# approved canary is enabled, it requires locked=true and zero broker writes.
"${compose[@]}" exec -T execution-worker \
  python -m tw_quant.execution_service healthcheck

# This is a distinct server-side Gateway/TLS/SNI path. It cannot substitute for
# the external runner's formal public hostname gate.
bash "${REPOSITORY}/deploy/lightsail/verify-gateway-path.sh"

if [[ "${MODE}" == "restart" ]]; then
  "${compose[@]}" restart market-api execution-worker gateway
  for service in market-api execution-worker gateway; do
    wait_for_health "${service}"
  done
  verify_runtime_dependency market-api
  verify_runtime_dependency execution-worker
  verify_container_security market-api 10001 none
  verify_container_security execution-worker 10001 none
  verify_container_security gateway 10000 NET_BIND_SERVICE
  "${compose[@]}" exec -T execution-worker \
    python -m tw_quant.execution_service healthcheck
  bash "${REPOSITORY}/deploy/lightsail/verify-gateway-path.sh"
elif [[ "${MODE}" != "verify" ]]; then
  echo "mode must be verify or restart" >&2
  exit 2
fi

echo "Production verification passed for ${COMMIT_SHA} (${MODE})"
