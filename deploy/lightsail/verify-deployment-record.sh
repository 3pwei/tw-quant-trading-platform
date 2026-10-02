#!/usr/bin/env bash
set -euo pipefail

EXPECTED_SHA="${1:?expected SHA is required}"
EXPECTED_MODE="${2:?expected deployment mode is required}"
INSTALL_ROOT="${INSTALL_ROOT:-/srv/trading-platform}"
DEPLOYMENT_DIR="${INSTALL_ROOT}/deployments"
DEPLOYMENT_RECORD="${DEPLOYMENT_DIR}/current.env"

if [[ ! "${EXPECTED_SHA}" =~ ^[0-9a-f]{40}$ ]]; then
  echo "Expected deployment revision is invalid" >&2
  exit 2
fi
case "${EXPECTED_MODE}" in
  deploy|rollback) ;;
  *) echo "Expected deployment mode is invalid" >&2; exit 2 ;;
esac

if [[ ! -d "${DEPLOYMENT_DIR}" || ! -f "${DEPLOYMENT_RECORD}" ]]; then
  echo "Production deployment record is missing" >&2
  exit 3
fi
if [[ "$(stat -c '%a' "${DEPLOYMENT_DIR}")" != "700" || "$(stat -c '%a' "${DEPLOYMENT_RECORD}")" != "600" ]]; then
  echo "Production deployment record permissions are invalid" >&2
  exit 3
fi

read_record_value() {
  local key="$1" count
  count="$(awk -F= -v key="${key}" '$1 == key { count += 1 } END { print count + 0 }' "${DEPLOYMENT_RECORD}")"
  [[ "${count}" == "1" ]] || {
    echo "Production deployment record has an invalid ${key} field" >&2
    return 1
  }
  awk -F= -v key="${key}" '$1 == key { print substr($0, index($0, "=") + 1) }' "${DEPLOYMENT_RECORD}"
}

recorded_sha="$(read_record_value deployed_sha)"
recorded_mode="$(read_record_value deployment_mode)"
previous_sha="$(read_record_value previous_known_good_sha)"
verified_at="$(read_record_value verified_at)"

[[ "${recorded_sha}" == "${EXPECTED_SHA}" ]] || {
  echo "Production deployment record revision does not match the approved revision" >&2
  exit 4
}
[[ "${recorded_mode}" == "${EXPECTED_MODE}" ]] || {
  echo "Production deployment record mode does not match the requested mode" >&2
  exit 4
}
[[ "${previous_sha}" =~ ^[0-9a-f]{40}$ ]] || {
  echo "Production deployment record previous revision is invalid" >&2
  exit 4
}
[[ -n "${verified_at}" ]] || {
  echo "Production deployment record verification timestamp is missing" >&2
  exit 4
}

echo "Verified Production deployment record for ${EXPECTED_SHA} (${EXPECTED_MODE})"
