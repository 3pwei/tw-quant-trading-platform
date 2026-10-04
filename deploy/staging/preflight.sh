#!/usr/bin/env bash
# Read-only checks, before staging provider transfer or host preparation.
set -euo pipefail
[[ "${INSTALL_ROOT:-/srv/trading-platform-staging}" == /srv/trading-platform-staging ]]
for tool in docker python3 curl timeout systemctl flock stat tar sha256sum useradd groupadd ss; do
  command -v "$tool" >/dev/null || { printf 'P8_PREFLIGHT=FAIL missing_tool=%s\n' "$tool"; exit 2; }
done
test -x /usr/lib/systemd/systemd-socket-proxyd
test -x /usr/sbin/nologin
timeout 10s docker version --format 'engine={{.Server.Version}}'
timeout 10s docker compose version --short
python3 --version
timeout 10s systemctl show --property=Version --value
# Identity validation runs read-only from this pipeline's exact helper before
# this script, without relying on an already-installed host bundle.
# An existing approved socket is allowed; a foreign loopback listener is not.
listeners="$(timeout 10s ss -H -ltn 'sport = :18080')" || {
  echo 'P8_PREFLIGHT=FAIL listener_inspection_failed'; exit 2;
}
if [[ -n "${listeners}" ]]; then
  read -r state receive_queue send_queue address peer rest <<< "${listeners}"
  [[ "${listeners}" != *$'\n'* && "${state}" == LISTEN && "${address}" == 127.0.0.1:18080 ]] || {
    echo 'P8_PREFLIGHT=FAIL foreign_listener'; exit 2;
  }
  systemctl is-active --quiet p8-staging-ingress.socket
  test "$(systemctl show --property=Listen --value p8-staging-ingress.socket)" = '127.0.0.1:18080 (Stream)'
fi
python3 - <<'PY'
import shutil
free = shutil.disk_usage('/srv').free
print('available_disk_bytes=' + str(free))
if free < 2 * 1024**3:
    raise SystemExit('P8_PREFLIGHT=FAIL insufficient_disk')
PY
echo 'P8_PREFLIGHT=PASS'
