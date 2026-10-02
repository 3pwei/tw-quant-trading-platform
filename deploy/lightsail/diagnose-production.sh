#!/usr/bin/env bash
set -euo pipefail

INSTALL_ROOT="${INSTALL_ROOT:-/srv/trading-platform}"
REPOSITORY="${INSTALL_ROOT}/repo"
COMPOSE_FILE="${REPOSITORY}/deploy/lightsail/docker-compose.yml"
COMPOSE_ENV="${INSTALL_ROOT}/config/compose.env"
compose=(docker compose --env-file "${COMPOSE_ENV}" -f "${COMPOSE_FILE}")

echo "sanitized_deployment_context_begin"
for service in market-api execution-worker gateway; do
  container_id="$("${compose[@]}" ps -aq "${service}" 2>/dev/null || true)"
  if [[ -z "${container_id}" ]]; then
    printf 'service=%s state=missing\n' "${service}"
    continue
  fi

  docker inspect --format \
    'service={{index .Config.Labels "com.docker.compose.service"}} state={{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} exit={{.State.ExitCode}} restart_count={{.RestartCount}} uid={{.Config.User}} readonly={{.HostConfig.ReadonlyRootfs}}' \
    "${container_id}" 2>/dev/null || printf 'service=%s state=inspect_failed\n' "${service}"

  docker inspect --format \
    '{{if .State.Health}}{{range .State.Health.Log}}health_event end={{.End}} exit={{.ExitCode}}{{println}}{{end}}{{end}}' \
    "${container_id}" 2>/dev/null | tail -n 10 || true
done
echo "sanitized_deployment_context_end"
