#!/usr/bin/env bash
set -euo pipefail

ACTION="${1:?deploy or rollback is required}"
MANIFEST="${2:-}"
RELEASE="${3:-}"
INSTALL_ROOT="${INSTALL_ROOT:-/srv/trading-platform-staging}"
[[ "${INSTALL_ROOT}" == /srv/trading-platform-staging && "${INSTALL_ROOT}" != *production* ]]

BUNDLE="${INSTALL_ROOT}/bundle"
DEPLOYMENTS="${INSTALL_ROOT}/deployments"
COMPOSE_FILE="${BUNDLE}/docker-compose.yml"
COMPOSE_ENV="${INSTALL_ROOT}/config/compose.env"
ACTIVE="${DEPLOYMENTS}/active-release.env"
CURRENT="${DEPLOYMENTS}/current.env"
PREVIOUS="${DEPLOYMENTS}/previous.env"

case "${ACTION}" in
  deploy)
    [[ -f "${MANIFEST}" && "${RELEASE}" =~ ^(known_good|candidate)$ ]]
    python "${BUNDLE}/candidate_manifest.py" verify "${MANIFEST}"
    target="$(mktemp "${DEPLOYMENTS}/.target.XXXXXX")"
    python "${BUNDLE}/candidate_manifest.py" emit-env "${MANIFEST}" --release "${RELEASE}" > "${target}"
    ;;
  rollback)
    [[ -f "${PREVIOUS}" ]]
    target="$(mktemp "${DEPLOYMENTS}/.target.XXXXXX")"
    cp "${PREVIOUS}" "${target}"
    ;;
  *) echo "action must be deploy or rollback" >&2; exit 2 ;;
esac
chmod 600 "${target}"

set -a
source "${target}"
set +a
for ref in "${STAGING_RUNTIME_IMAGE}" "${STAGING_GATEWAY_IMAGE}"; do
  docker image inspect "${ref}" >/dev/null
done

prior=""
if [[ -f "${CURRENT}" ]]; then
  prior="$(mktemp "${DEPLOYMENTS}/.prior.XXXXXX")"
  cp "${CURRENT}" "${prior}"
  chmod 600 "${prior}"
fi

restore_prior() {
  local status=$?
  if [[ ${status} -ne 0 && -n "${prior}" && -f "${prior}" ]]; then
    cp "${prior}" "${ACTIVE}"
    chmod 600 "${ACTIVE}"
    docker compose --env-file "${COMPOSE_ENV}" --env-file "${ACTIVE}" \
      -f "${COMPOSE_FILE}" up --no-build --pull never --detach --remove-orphans --force-recreate || true
    "${BUNDLE}/verify.sh" verify || true
  fi
  rm -f "${target}" "${prior:-}"
  exit ${status}
}
trap restore_prior EXIT

cp "${target}" "${ACTIVE}"
chmod 600 "${ACTIVE}"
compose=(docker compose --env-file "${COMPOSE_ENV}" --env-file "${ACTIVE}" -f "${COMPOSE_FILE}")

prepare_volume() {
  local name="$1" uid="$2" gid="$3" mountpoint
  docker volume create "${name}" >/dev/null
  mountpoint="$(docker volume inspect --format '{{.Mountpoint}}' "${name}")"
  chown -R "${uid}:${gid}" "${mountpoint}"
  chmod 0750 "${mountpoint}"
}
prepare_volume platform-staging_staging-data 10001 10001
prepare_volume platform-staging_execution-health 10001 10001
prepare_volume platform-staging_gateway-data 10000 10000
prepare_volume platform-staging_gateway-config 10000 10000

if ! docker run --rm --network none \
  --mount source=platform-staging_staging-data,target=/data \
  "${STAGING_RUNTIME_IMAGE}" \
  python -m tw_quant.synthetic_data --output /data/synthetic.csv; then
  echo "Synthetic staging data preparation failed" >&2
  exit 1
fi

"${compose[@]}" config --quiet
if "${compose[@]}" config | grep -qE '(^|/)(srv/trading-platform/|platform-production|production\.sqlite3)'; then
  echo "Production reference detected in staging configuration" >&2
  exit 4
fi
"${compose[@]}" up --no-build --pull never --detach --remove-orphans --force-recreate
"${BUNDLE}/verify.sh" verify
"${BUNDLE}/verify.sh" restart

record="$(mktemp "${DEPLOYMENTS}/.record.XXXXXX")"
cat "${target}" > "${record}"
{
  printf 'DEPLOYMENT_MODE=%s\n' "${ACTION}"
  printf 'VERIFIED_AT=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} >> "${record}"
chmod 600 "${record}"
if [[ -f "${CURRENT}" ]]; then
  cp "${CURRENT}" "${PREVIOUS}.tmp"
  chmod 600 "${PREVIOUS}.tmp"
  mv "${PREVIOUS}.tmp" "${PREVIOUS}"
fi
mv "${record}" "${CURRENT}"
cp "${CURRENT}" "${ACTIVE}"
chmod 600 "${ACTIVE}"

trap - EXIT
rm -f "${target}" "${prior:-}"
echo "P8 staging ${ACTION}: PASS release=${RELEASE:-previous} configuration=${CONFIGURATION_IDENTITY}"
