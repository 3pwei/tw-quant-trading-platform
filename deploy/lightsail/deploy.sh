#!/usr/bin/env bash
set -euo pipefail

COMMIT_SHA="${1:?commit SHA is required}"
DEPLOYMENT_MODE="${2:-deploy}"
INSTALL_ROOT="${INSTALL_ROOT:-/srv/trading-platform}"
REPOSITORY="${INSTALL_ROOT}/repo"
COMPOSE_FILE="${REPOSITORY}/deploy/lightsail/docker-compose.yml"
COMPOSE_ENV="${INSTALL_ROOT}/config/compose.env"
EXECUTION_ENV="${INSTALL_ROOT}/config/execution.env"


if [[ ! "${COMMIT_SHA}" =~ ^[0-9a-f]{40}$ ]]; then
  echo "Invalid commit SHA" >&2
  exit 2
fi

case "${DEPLOYMENT_MODE}" in
  deploy|rollback) ;;
  *) echo "deployment mode must be deploy or rollback" >&2; exit 2 ;;
esac

PREVIOUS_KNOWN_GOOD_SHA="$(git -C "${REPOSITORY}" rev-parse HEAD)"
if ! git -C "${REPOSITORY}" cat-file -e "${COMMIT_SHA}^{commit}"; then
  echo "Approved commit is not staged in the deployment repository" >&2
  exit 3
fi
git -C "${REPOSITORY}" checkout --detach "${COMMIT_SHA}"
export DEPLOY_COMMIT_SHA="${COMMIT_SHA}"

# Compose resolves every service env_file before running even a read-only
# command. Materialize the locked execution config before the first Compose use.
"${REPOSITORY}/deploy/lightsail/prepare-host.sh"

sanitize_runtime_output() {
  sed -E \
    -e 's/((KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)[A-Z0-9_]*[=:])[[:space:]]*[^[:space:]]+/\1[REDACTED]/Ig' \
    -e 's/([A-Za-z0-9._%+-]+)@[A-Za-z0-9.-]+/redacted@example.invalid/g'
}

dump_startup_diagnostics() {
  local service container_id
  echo "Sanitized container startup diagnostics" >&2
  docker compose --env-file "${COMPOSE_ENV}" -f "${COMPOSE_FILE}" ps >&2 || true
  for service in market-api gateway; do
    container_id="$(docker compose --env-file "${COMPOSE_ENV}" -f "${COMPOSE_FILE}" \
      ps -aq "${service}" 2>/dev/null || true)"
    [[ -n "${container_id}" ]] || continue
    docker inspect --format \
      'service={{index .Config.Labels "com.docker.compose.service"}} status={{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} uid={{.Config.User}} readonly={{.HostConfig.ReadonlyRootfs}}' \
      "${container_id}" 2>&1 | sanitize_runtime_output >&2 || true
    docker inspect --format '{{if .State.Health}}{{range .State.Health.Log}}{{.End}} exit={{.ExitCode}} {{.Output}}{{println}}{{end}}{{end}}' \
      "${container_id}" 2>&1 | tail -n 40 | sanitize_runtime_output >&2 || true
    docker logs --tail 100 "${container_id}" 2>&1 | sanitize_runtime_output >&2 || true
  done
}

if docker compose --env-file "${COMPOSE_ENV}" -f "${COMPOSE_FILE}" \
  ps --status running --services | grep -qx market-api; then
  docker compose --env-file "${COMPOSE_ENV}" -f "${COMPOSE_FILE}" \
    exec -T -e DEPLOY_COMMIT_SHA="${COMMIT_SHA}" market-api python - <<'PY'
import os
import sqlite3
from pathlib import Path

source_path = Path(os.environ.get("MARKET_DB_PATH", "/data/platform.sqlite3"))
if source_path.exists():
    revision = os.environ["DEPLOY_COMMIT_SHA"][:12]
    backup_path = source_path.with_name(
        f"{source_path.stem}.backup-{revision}{source_path.suffix}"
    )
    source = sqlite3.connect(source_path)
    backup = sqlite3.connect(backup_path)
    try:
        source.backup(backup)
        result = backup.execute("PRAGMA integrity_check").fetchone()
        if result != ("ok",):
            raise RuntimeError(f"SQLite backup integrity check failed: {result!r}")
    finally:
        backup.close()
        source.close()
    print(f"SQLite backup created and verified: {backup_path}")
PY
fi

if [[ ! -f "${EXECUTION_ENV}" || "$(stat -c '%a' "${EXECUTION_ENV}")" != "600" ]]; then
  echo "execution.env must exist as a regular file with mode 600" >&2
  exit 4
fi
if find "${INSTALL_ROOT}/secrets" -maxdepth 1 -type f \
  \( ! -user 10001 -o ! -group 10001 -o -perm /077 \) \
  -print -quit | grep -q .; then
  echo "execution secret files must be owned by 10001:10001 with no group/other permissions" >&2
  exit 4
fi

docker compose \
  --env-file "${COMPOSE_ENV}" \
  -f "${COMPOSE_FILE}" \
  build

prepare_named_volume() {
  local volume_name="$1" uid="$2" gid="$3" mode="$4" mountpoint
  docker volume create "${volume_name}" >/dev/null
  mountpoint="$(docker volume inspect --format '{{.Mountpoint}}' "${volume_name}")"
  [[ -n "${mountpoint}" && -d "${mountpoint}" ]]
  chown -R "${uid}:${gid}" "${mountpoint}"
  chmod "${mode}" "${mountpoint}"
}

# Migrate existing named volumes before the first non-root validation container.
# This is a host-side, narrowly scoped ownership change; application containers
# remain non-root throughout validation and runtime.
prepare_named_volume platform-production_market-data 10001 10001 0750
prepare_named_volume platform-production_execution-health 10001 10001 0750
prepare_named_volume platform-production_caddy-data 10000 10000 0750
prepare_named_volume platform-production_caddy-config 10000 10000 0750

# Validate the target image with the server's real environment before replacing
# the currently healthy containers. A missing production auth setting must stop
# the deployment instead of silently enabling the local-development bypass.
docker compose \
  --env-file "${COMPOSE_ENV}" \
  -f "${COMPOSE_FILE}" \
  run --rm --no-deps -T market-api python -c \
  'from tw_quant.live.settings import LiveSettings; LiveSettings.from_env().validate()'

# Validate the isolated execution configuration. Locked/disabled is a valid
# service state; this command never logs in, activates a CA, or submits orders.
docker compose \
  --env-file "${COMPOSE_ENV}" \
  -f "${COMPOSE_FILE}" \
  run --rm --no-deps -T execution-worker \
  python -m tw_quant.execution_service validate

if ! docker compose \
  --env-file "${COMPOSE_ENV}" \
  -f "${COMPOSE_FILE}" \
  up --no-build --detach --remove-orphans --force-recreate; then
  dump_startup_diagnostics
  exit 1
fi

for service in market-api execution-worker gateway; do
  built_image="$(docker compose \
    --env-file "${COMPOSE_ENV}" \
    -f "${COMPOSE_FILE}" images -q "${service}")"
  container_id="$(docker compose \
    --env-file "${COMPOSE_ENV}" \
    -f "${COMPOSE_FILE}" ps -q "${service}")"
  if [[ -z "${built_image}" || -z "${container_id}" ]]; then
    echo "${service} image or running container is missing" >&2
    exit 1
  fi
  expected_image="$(docker image inspect --format '{{.Id}}' "${built_image}")"
  running_image="$(docker inspect --format '{{.Image}}' "${container_id}")"
  if [[ "${running_image}" != "${expected_image}" ]]; then
    echo "${service} is not running the newly built image" >&2
    exit 1
  fi
done

execution_container="$(docker compose \
  --env-file "${COMPOSE_ENV}" \
  -f "${COMPOSE_FILE}" ps -q execution-worker)"
published_ports="$(docker inspect --format '{{json .NetworkSettings.Ports}}' \
  "${execution_container}")"
if [[ "${published_ports}" != "{}" ]]; then
  echo "execution-worker unexpectedly exposes a network port" >&2
  exit 1
fi

bash "${REPOSITORY}/deploy/lightsail/verify-production.sh" "${COMMIT_SHA}" verify
bash "${REPOSITORY}/deploy/lightsail/verify-production.sh" "${COMMIT_SHA}" restart

install -d -m 700 "${INSTALL_ROOT}/deployments"
deployment_record="${INSTALL_ROOT}/deployments/current.env"
temporary_record="$(mktemp "${INSTALL_ROOT}/deployments/.current.XXXXXX")"
{
  printf 'deployment_mode=%s\n' "${DEPLOYMENT_MODE}"
  printf 'deployed_sha=%s\n' "${COMMIT_SHA}"
  printf 'previous_known_good_sha=%s\n' "${PREVIOUS_KNOWN_GOOD_SHA}"
  printf 'public_core_version=1.2.0\n'
  printf 'public_core_sha256=63645e42755068c308d66d74ded5133395dfef816360dc8106e0bbc247ee49bd\n'
  printf 'verified_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} > "${temporary_record}"
chmod 600 "${temporary_record}"
mv "${temporary_record}" "${deployment_record}"

docker compose \
  --env-file "${COMPOSE_ENV}" \
  -f "${COMPOSE_FILE}" \
  ps
