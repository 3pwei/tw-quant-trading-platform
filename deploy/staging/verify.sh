#!/usr/bin/env bash
set -euo pipefail

MODE="${1:-verify}"
INSTALL_ROOT="${INSTALL_ROOT:-/srv/trading-platform-staging}"
[[ "${INSTALL_ROOT}" == /srv/trading-platform-staging && "${INSTALL_ROOT}" != *production* ]]
case "${MODE}" in verify|restart) ;; *) echo "mode must be verify or restart" >&2; exit 2 ;; esac

COMPOSE_FILE="${INSTALL_ROOT}/bundle/docker-compose.yml"
COMPOSE_ENV="${INSTALL_ROOT}/config/compose.env"
RELEASE_ENV="${INSTALL_ROOT}/deployments/active-release.env"
set -a
source "${RELEASE_ENV}"
set +a
compose=(docker compose --env-file "${COMPOSE_ENV}" --env-file "${RELEASE_ENV}" -f "${COMPOSE_FILE}")

wait_for_health() {
  local service="$1" container_id state
  for _ in {1..45}; do
    container_id="$("${compose[@]}" ps -q "${service}")"
    if [[ -n "${container_id}" ]]; then
      state="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "${container_id}")"
      [[ "${state}" == healthy || "${state}" == running ]] && return 0
      [[ "${state}" == unhealthy || "${state}" == exited || "${state}" == dead ]] && return 1
    fi
    sleep 2
  done
  return 1
}

verify_image() {
  local service="$1" exact_ref="$2" expected_config="$3" container_id running_config local_config repo_digests
  container_id="$("${compose[@]}" ps -q "${service}")"
  running_config="$(docker inspect --format '{{.Image}}' "${container_id}")"
  local_config="$(docker image inspect --format '{{.Id}}' "${exact_ref}")"
  repo_digests="$(docker image inspect --format '{{json .RepoDigests}}' "${exact_ref}")"
  [[ "${running_config}" == "${local_config}" && "${local_config}" == "${expected_config}" ]]
  [[ "${repo_digests}" == *"${exact_ref}"* ]]
}

verify_labels() {
  local ref="$1" config_expected="$2"
  [[ "$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "${ref}")" == "${PLATFORM_SOURCE_SHA}" ]]
  [[ "$(docker image inspect --format '{{index .Config.Labels "io.tw-quant.pipeline.revision"}}' "${ref}")" == "${PIPELINE_REVISION}" ]]
  [[ "$(docker image inspect --format '{{index .Config.Labels "io.tw-quant.configuration.identity"}}' "${ref}")" == "${config_expected}" ]]
  [[ "$(docker image inspect --format '{{index .Config.Labels "io.tw-quant.core.sha256"}}' "${ref}")" == "${CORE_WHEEL_SHA256}" ]]
  [[ "$(docker image inspect --format '{{index .Config.Labels "io.tw-quant.private-provider.sha256"}}' "${ref}")" == "${PRIVATE_PROVIDER_WHEEL_SHA256}" ]]
  [[ "$(docker image inspect --format '{{index .Config.Labels "io.tw-quant.p7.acceptance"}}' "${ref}")" == "${P7_ACCEPTANCE_SHA}" ]]
}

verify_gateway_labels() {
  [[ "$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "${STAGING_GATEWAY_IMAGE}")" == "${PLATFORM_SOURCE_SHA}" ]]
  [[ "$(docker image inspect --format '{{index .Config.Labels "io.tw-quant.pipeline.revision"}}' "${STAGING_GATEWAY_IMAGE}")" == "${PIPELINE_REVISION}" ]]
}

verify_container_security() {
  local service="$1" uid="$2" container_id
  container_id="$("${compose[@]}" ps -q "${service}")"
  [[ "$(docker exec "${container_id}" id -u)" == "${uid}" ]]
  [[ "$(docker inspect --format '{{.HostConfig.ReadonlyRootfs}}' "${container_id}")" == true ]]
  [[ "$(docker inspect --format '{{json .HostConfig.SecurityOpt}}' "${container_id}")" == *no-new-privileges:true* ]]
  [[ "$(docker inspect --format '{{json .HostConfig.CapDrop}}' "${container_id}")" == *ALL* ]]
}

verify_once() {
  local service execution_container ports network_mode health
  for service in market-api execution-worker gateway; do wait_for_health "${service}"; done
  verify_image market-api "${STAGING_RUNTIME_IMAGE}" "${STAGING_RUNTIME_CONFIG_DIGEST}"
  verify_image execution-worker "${STAGING_RUNTIME_IMAGE}" "${STAGING_RUNTIME_CONFIG_DIGEST}"
  verify_image gateway "${STAGING_GATEWAY_IMAGE}" "${STAGING_GATEWAY_CONFIG_DIGEST}"
  verify_labels "${STAGING_RUNTIME_IMAGE}" "${CONFIGURATION_IDENTITY}"
  verify_gateway_labels
  verify_container_security market-api 10001
  verify_container_security execution-worker 10001
  verify_container_security gateway 10000

  execution_container="$("${compose[@]}" ps -q execution-worker)"
  ports="$(docker inspect --format '{{json .NetworkSettings.Ports}}' "${execution_container}")"
  network_mode="$(docker inspect --format '{{.HostConfig.NetworkMode}}' "${execution_container}")"
  [[ "${ports}" == "{}" && "${network_mode}" == none ]]
  "${compose[@]}" exec -T execution-worker python -m tw_quant.execution_service healthcheck
  health="$("${compose[@]}" exec -T execution-worker cat /run/tw-quant-execution/health.json)"
  python -c 'import json,sys; d=json.load(sys.stdin); assert d["locked"] is True; assert d["external_order_calls"] == 0; assert d["external_cancel_calls"] == 0' <<<"${health}"
  curl --fail --silent --show-error http://127.0.0.1:18080/healthz | grep -qx ok
  curl --fail --silent --show-error http://127.0.0.1:18080/health/live >/dev/null
  docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges \
    --tmpfs /tmp:rw,noexec,nosuid,nodev,size=128m,uid=10001,gid=10001 \
    --mount "type=bind,source=${INSTALL_ROOT}/provider/factory,target=/run/staging-provider/factory,readonly" \
    --mount "type=bind,source=${INSTALL_ROOT}/bundle/runtime_acceptance.py,target=/runtime_acceptance.py,readonly" \
    --env "PRIVATE_PROVIDER_WHEEL_SHA256=${PRIVATE_PROVIDER_WHEEL_SHA256}" \
    "${STAGING_RUNTIME_IMAGE}" python /runtime_acceptance.py
  docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges \
    --tmpfs /tmp:rw,noexec,nosuid,nodev,size=128m,uid=10001,gid=10001 \
    --mount "type=bind,source=${INSTALL_ROOT}/bundle/runtime-tests,target=/runtime-tests,readonly" \
    "${STAGING_RUNTIME_IMAGE}" \
    python -m unittest discover -s /runtime-tests -p 'test_*.py' -v
}

verify_once
if [[ "${MODE}" == restart ]]; then
  "${compose[@]}" restart market-api execution-worker gateway
  verify_once
fi
echo "P8 staging runtime verification: PASS (${MODE})"
