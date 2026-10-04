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

fail_check() {
  local check="$1" service="$2" expected="$3" actual="$4"
  printf 'P8_VERIFY_FAIL check=%s service=%s expected=%s actual=%s\n' \
    "${check}" "${service}" "${expected}" "${actual}" >&2
  return 1
}

wait_for_health() {
  local service="$1" container_id state=missing
  for _ in {1..45}; do
    container_id="$("${compose[@]}" ps -q "${service}")"
    if [[ -n "${container_id}" ]]; then
      state="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "${container_id}")"
      [[ "${state}" == healthy || "${state}" == running ]] && return 0
      if [[ "${state}" == unhealthy || "${state}" == exited || "${state}" == dead ]]; then
        fail_check service-health-terminal "${service}" healthy "${state}"
      fi
    fi
    sleep 2
  done
  fail_check service-health-timeout "${service}" healthy "${state}"
}

verify_image() {
  local service="$1" exact_ref="$2" expected_config="$3"
  local container_id running_config local_config repo_digests tagged_ref repository canonical_ref
  container_id="$("${compose[@]}" ps -q "${service}")"
  running_config="$(docker inspect --format '{{.Image}}' "${container_id}")"
  local_config="$(docker image inspect --format '{{.Id}}' "${exact_ref}")"
  repo_digests="$(docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "${exact_ref}")"
  tagged_ref="${exact_ref%@*}"
  repository="${tagged_ref%:*}"
  canonical_ref="${repository}@${exact_ref##*@}"
  [[ "${running_config}" == "${local_config}" ]] || \
    fail_check image-running-config-mismatch "${service}" "${local_config}" "${running_config}"
  [[ "${local_config}" == "${expected_config}" ]] || \
    fail_check local-image-config-mismatch "${service}" "${expected_config}" "${local_config}"
  grep -Fxq "${canonical_ref}" <<<"${repo_digests}" || \
    fail_check canonical-repodigest-mismatch "${service}" "${exact_ref##*@}" missing
}

verify_labels() {
  local service="$1" ref="$2" config_expected="$3" actual
  actual="$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "${ref}")"
  [[ "${actual}" == "${PLATFORM_SOURCE_SHA}" ]] || \
    fail_check source-revision-mismatch "${service}" "${PLATFORM_SOURCE_SHA}" "${actual}"
  actual="$(docker image inspect --format '{{index .Config.Labels "io.tw-quant.pipeline.revision"}}' "${ref}")"
  [[ "${actual}" == "${PIPELINE_REVISION}" ]] || \
    fail_check pipeline-revision-mismatch "${service}" "${PIPELINE_REVISION}" "${actual}"
  actual="$(docker image inspect --format '{{index .Config.Labels "io.tw-quant.configuration.identity"}}' "${ref}")"
  [[ "${actual}" == "${config_expected}" ]] || \
    fail_check configuration-identity-mismatch "${service}" "${config_expected}" "${actual}"
  actual="$(docker image inspect --format '{{index .Config.Labels "io.tw-quant.core.sha256"}}' "${ref}")"
  [[ "${actual}" == "${CORE_WHEEL_SHA256}" ]] || \
    fail_check core-digest-mismatch "${service}" "${CORE_WHEEL_SHA256}" "${actual}"
  actual="$(docker image inspect --format '{{index .Config.Labels "io.tw-quant.private-provider.sha256"}}' "${ref}")"
  [[ "${actual}" == "${PRIVATE_PROVIDER_WHEEL_SHA256}" ]] || \
    fail_check private-provider-digest-mismatch "${service}" "${PRIVATE_PROVIDER_WHEEL_SHA256}" "${actual}"
  actual="$(docker image inspect --format '{{index .Config.Labels "io.tw-quant.p7.acceptance"}}' "${ref}")"
  [[ "${actual}" == "${P7_ACCEPTANCE_SHA}" ]] || \
    fail_check p7-acceptance-mismatch "${service}" "${P7_ACCEPTANCE_SHA}" "${actual}"
}

verify_gateway_labels() {
  local actual
  actual="$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "${STAGING_GATEWAY_IMAGE}")"
  [[ "${actual}" == "${PLATFORM_SOURCE_SHA}" ]] || \
    fail_check source-revision-mismatch gateway "${PLATFORM_SOURCE_SHA}" "${actual}"
  actual="$(docker image inspect --format '{{index .Config.Labels "io.tw-quant.pipeline.revision"}}' "${STAGING_GATEWAY_IMAGE}")"
  [[ "${actual}" == "${PIPELINE_REVISION}" ]] || \
    fail_check pipeline-revision-mismatch gateway "${PIPELINE_REVISION}" "${actual}"
}

verify_container_security() {
  local service="$1" uid="$2" container_id actual
  container_id="$("${compose[@]}" ps -q "${service}")"
  actual="$(docker exec "${container_id}" id -u)"
  [[ "${actual}" == "${uid}" ]] || fail_check uid-mismatch "${service}" "${uid}" "${actual}"
  actual="$(docker inspect --format '{{.HostConfig.ReadonlyRootfs}}' "${container_id}")"
  [[ "${actual}" == true ]] || fail_check readonly-rootfs-mismatch "${service}" true "${actual}"
  actual="$(docker inspect --format '{{json .HostConfig.SecurityOpt}}' "${container_id}")"
  [[ "${actual}" == *no-new-privileges:true* ]] || \
    fail_check no-new-privileges-missing "${service}" true false
  actual="$(docker inspect --format '{{json .HostConfig.CapDrop}}' "${container_id}")"
  [[ "${actual}" == *ALL* ]] || fail_check cap-drop-all-missing "${service}" true false
}

verify_once() {
  local service execution_container ports network_mode health health_fields
  local locked_ok locked_actual order_ok order_actual cancel_ok cancel_actual
  for service in market-api execution-worker gateway; do wait_for_health "${service}"; done
  verify_image market-api "${STAGING_RUNTIME_IMAGE}" "${STAGING_RUNTIME_CONFIG_DIGEST}"
  verify_image execution-worker "${STAGING_RUNTIME_IMAGE}" "${STAGING_RUNTIME_CONFIG_DIGEST}"
  verify_image gateway "${STAGING_GATEWAY_IMAGE}" "${STAGING_GATEWAY_CONFIG_DIGEST}"
  verify_labels market-api "${STAGING_RUNTIME_IMAGE}" "${CONFIGURATION_IDENTITY}"
  verify_gateway_labels
  verify_container_security market-api 10001
  verify_container_security execution-worker 10001
  verify_container_security gateway 10000

  execution_container="$("${compose[@]}" ps -q execution-worker)"
  ports="$(docker inspect --format '{{json .NetworkSettings.Ports}}' "${execution_container}")"
  network_mode="$(docker inspect --format '{{.HostConfig.NetworkMode}}' "${execution_container}")"
  [[ "${ports}" == "{}" ]] || fail_check execution-ports-exposed execution-worker '{}' "${ports}"
  [[ "${network_mode}" == none ]] || \
    fail_check execution-network-mode-mismatch execution-worker none "${network_mode}"
  "${compose[@]}" exec -T execution-worker python -m tw_quant.execution_service healthcheck || \
    fail_check execution-not-locked execution-worker true false
  health="$("${compose[@]}" exec -T execution-worker cat /run/tw-quant-execution/health.json)"
  health_fields="$(python3 -c 'import json,sys; d=json.load(sys.stdin); locked=d.get("locked"); order=d.get("external_order_calls"); cancel=d.get("external_cancel_calls"); safe=lambda value: str(value).lower() if isinstance(value, (bool, int, float)) else "invalid"; print(str(locked is True).lower(), safe(locked), str(order == 0).lower(), safe(order), str(cancel == 0).lower(), safe(cancel))' <<<"${health}")"
  read -r locked_ok locked_actual order_ok order_actual cancel_ok cancel_actual <<<"${health_fields}"
  [[ "${locked_ok}" == true ]] || \
    fail_check execution-not-locked execution-worker true "${locked_actual}"
  [[ "${order_ok}" == true ]] || \
    fail_check external-order-call-detected execution-worker 0 "${order_actual}"
  [[ "${cancel_ok}" == true ]] || \
    fail_check external-cancel-call-detected execution-worker 0 "${cancel_actual}"
  curl --fail --silent --show-error http://127.0.0.1:18080/healthz | grep -qx ok || \
    fail_check market-health-failed market-api true false
  curl --fail --silent --show-error http://127.0.0.1:18080/health/live >/dev/null || \
    fail_check market-health-failed market-api true false
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
