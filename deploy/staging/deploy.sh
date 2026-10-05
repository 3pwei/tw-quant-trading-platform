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

fail_deploy() {
  local check="$1" service="$2" expected="$3" actual="$4"
  printf 'P8_DEPLOY_FAIL check=%s service=%s expected=%s actual=%s\n' \
    "${check}" "${service}" "${expected}" "${actual}" >&2
  return 1
}

case "${ACTION}" in
  deploy)
    [[ -f "${MANIFEST}" && "${RELEASE}" =~ ^(known_good|candidate)$ ]]
    python3 "${BUNDLE}/candidate_manifest.py" verify "${MANIFEST}"
    target="$(mktemp "${DEPLOYMENTS}/.target.XXXXXX")"
    python3 "${BUNDLE}/candidate_manifest.py" emit-env "${MANIFEST}" --release "${RELEASE}" > "${target}"
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
export P8_PHASE="${ACTION}-${RELEASE:-rollback}"
for ref in "${STAGING_RUNTIME_IMAGE}" "${STAGING_GATEWAY_IMAGE}"; do
  docker image inspect "${ref}" >/dev/null
done

prior=""
prior_previous=""
if [[ -f "${CURRENT}" ]]; then
  prior="$(mktemp "${DEPLOYMENTS}/.prior.XXXXXX")"
  cp "${CURRENT}" "${prior}"
  chmod 600 "${prior}"
fi
if [[ -f "${PREVIOUS}" ]]; then
  prior_previous="$(mktemp "${DEPLOYMENTS}/.previous.XXXXXX")"
  cp "${PREVIOUS}" "${prior_previous}"
  chmod 600 "${prior_previous}"
fi

restore_prior() {
  local status=$?
  trap - EXIT
  # Preserve the original deployment failure, even when compensation also fails.
  set +e
  if [[ ${status} -ne 0 ]]; then
    # Capture the failed generation before any compensation changes containers.
    # Diagnostic failure cannot replace the original failure or prevent restore.
    timeout --signal=TERM --kill-after=2s 30s python3 "${BUNDLE}/diagnostics.py" \
      --pre-compensation --phase "${P8_PHASE}" >/dev/null 2>&1 || true
  fi
  if [[ ${status} -ne 0 && -n "${prior}" && -f "${prior}" ]]; then
    if (
      cp "${prior}" "${ACTIVE}.restore" &&
      chmod 600 "${ACTIVE}.restore" &&
      mv "${ACTIVE}.restore" "${ACTIVE}" &&
      # Shell interpolation wins over --env-file: replace every exported B value.
      set -a &&
      source "${ACTIVE}" &&
      set +a &&
      export P8_PHASE=compensation &&
      docker compose --env-file "${COMPOSE_ENV}" --env-file "${ACTIVE}" \
        -f "${COMPOSE_FILE}" up --no-build --pull never --detach --remove-orphans --force-recreate &&
      "${BUNDLE}/verify.sh" verify &&
      cp "${prior}" "${CURRENT}.restore" &&
      chmod 600 "${CURRENT}.restore" &&
      mv "${CURRENT}.restore" "${CURRENT}" &&
      if [[ -n "${prior_previous}" ]]; then
        cp "${prior_previous}" "${PREVIOUS}.restore" &&
        chmod 600 "${PREVIOUS}.restore" &&
        mv "${PREVIOUS}.restore" "${PREVIOUS}"
      else
        rm -f "${PREVIOUS}"
      fi
    ); then
      echo 'P8_RESTORE=PASS acceptance=false' >&2
    else
      echo 'P8_RESTORE=FAIL acceptance=false operator_intervention_required=true' >&2
    fi
  fi
  rm -f "${target}" "${prior:-}" "${prior_previous:-}"
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

# A persisted, permanently locked synthetic target proves restart continuity.
docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges \
  --mount source=platform-staging_staging-data,target=/data \
  --mount "type=bind,source=${BUNDLE}/durable_probe.py,target=/durable_probe.py,readonly" \
  --env BROKER_PROVIDER=disabled --env LIVE_TRADING_ENABLED=false \
  "${STAGING_RUNTIME_IMAGE}" python /durable_probe.py seed >/dev/null

"${compose[@]}" config --quiet
if "${compose[@]}" config | grep -qE '(^|/)(srv/trading-platform/|platform-production|production\.sqlite3)'; then
  echo "Production reference detected in staging configuration" >&2
  exit 4
fi
if ! "${compose[@]}" up --no-build --pull never --detach --remove-orphans --force-recreate; then
  fail_deploy compose-up-failed compose success failed
fi
if ! "${BUNDLE}/verify.sh" verify; then
  fail_deploy verifier-invocation-failed verify success failed
fi
if ! "${BUNDLE}/verify.sh" restart; then
  fail_deploy verifier-invocation-failed restart success failed
fi

record="$(mktemp "${DEPLOYMENTS}/.record.XXXXXX")"
# A rollback target is a prior committed record and already carries metadata.
# Replace those fields, retaining the immutable release identity exactly once.
while IFS= read -r line; do
  case "${line}" in DEPLOYMENT_MODE=*|VERIFIED_AT=*) continue ;; esac
  printf '%s\n' "${line}"
done < "${target}" > "${record}"
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
rm -f "${target}" "${prior:-}" "${prior_previous:-}"
python3 "${BUNDLE}/acceptance_evidence.py" capture --event "${P8_PHASE}/committed"
echo "P8 staging ${ACTION}: PASS release=${RELEASE:-previous} configuration=${CONFIGURATION_IDENTITY}"
