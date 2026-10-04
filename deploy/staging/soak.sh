#!/usr/bin/env bash
set -euo pipefail
MINUTES="${1:-60}"
[[ "${MINUTES}" =~ ^[0-9]+$ && "${MINUTES}" -ge 30 && "${MINUTES}" -le 360 ]]
INSTALL_ROOT="${INSTALL_ROOT:-/srv/trading-platform-staging}"
[[ "${INSTALL_ROOT}" == /srv/trading-platform-staging && "${INSTALL_ROOT}" != *production* ]]
BUNDLE="${INSTALL_ROOT}/bundle"
python3 "${BUNDLE}/acceptance_evidence.py" soak --minutes "${MINUTES}"
python3 "${BUNDLE}/acceptance_evidence.py" final
echo "P8_STAGING_SOAK=PASS minutes=${MINUTES}"
